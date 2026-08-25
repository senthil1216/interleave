from __future__ import annotations

from interleave.adapter import FakeDownstreamAdapter
from interleave.approval import ApprovalService
from interleave.clock import Clock
from interleave.models import EventType, Operation, Outcome, Request, State
from interleave.normalize import normalize
from interleave.policy import Policy
from interleave.store import ConflictError, OperationStore


class Gateway:
    """Orchestrates: policy -> normalize -> get_or_create -> approval ->
    (revalidate approval+policy + execute) in one transaction -> ledger -> terminal state."""

    def __init__(
        self,
        store: OperationStore,
        clock: Clock,
        policy: Policy,
        downstream: FakeDownstreamAdapter,
        approval_ttl_seconds: float = 900.0,
        max_attempts: int = 3,
        deadline_seconds: float = 5.0,
    ):
        self.store = store
        self.clock = clock
        self.policy = policy
        self.downstream = downstream
        self.approvals = ApprovalService(store, clock, ttl_seconds=approval_ttl_seconds)
        self.max_attempts = max_attempts
        self.deadline_seconds = deadline_seconds

    def submit(self, request: Request) -> Operation:
        normalized_intent = normalize(request)
        result = self.store.get_or_create(request, normalized_intent)
        op = result.operation
        if result.created:
            if not self.policy.check(op):
                self.store.cas(op.id, (State.PENDING_APPROVAL,), State.DENIED)
                self.store.audit(op.id, EventType.POLICY_DENIED_INTAKE, {})
                self.store.audit(op.id, EventType.OPERATION_DENIED, {"reason": "policy_intake"})
            else:
                self.store.audit(op.id, EventType.APPROVAL_REQUESTED, {"epoch": op.approval_epoch})
        return self.store.get_by_id(op.id)

    def approve(self, operation_id: int) -> Operation:
        self.approvals.approve(operation_id)
        return self.store.get_by_id(operation_id)

    def revoke(self, operation_id: int) -> Operation:
        self.approvals.revoke(operation_id)
        return self.store.get_by_id(operation_id)

    def revalidate_and_start_attempt(self, operation_id: int) -> tuple[Operation, int | None]:
        """Revalidate approval + policy, live, per attempt. The revalidation check and the
        `executing` transition + intent-record write happen in one transaction, so there's
        no window between 'confirmed approved' and 'committed to acting' for a concurrent
        revoke to land in — closed locally, not just narrowed."""
        self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            op = self.store.get_by_id(operation_id)
            now = self.clock.now()

            if op.state == State.APPROVED and op.expires_at is not None and now > op.expires_at:
                self.store.cas(operation_id, (State.APPROVED,), State.EXPIRED)
                self.store.audit(operation_id, EventType.APPROVAL_EXPIRED, {})
                self.store.audit(operation_id, EventType.OPERATION_EXPIRED, {})
                self.store.conn.execute("COMMIT")
                return self.store.get_by_id(operation_id), None

            if op.state != State.APPROVED:
                self.store.conn.execute("COMMIT")
                return op, None

            if not self.policy.check(op):
                self.store.cas(operation_id, (State.APPROVED,), State.DENIED)
                self.store.audit(operation_id, EventType.POLICY_DENIED_REVALIDATION, {})
                self.store.audit(operation_id, EventType.OPERATION_DENIED, {"reason": "policy_revalidation"})
                self.store.conn.execute("COMMIT")
                return self.store.get_by_id(operation_id), None

            attempt_no = self.store.next_attempt_no(operation_id)
            self.store.cas(operation_id, (State.APPROVED,), State.EXECUTING)
            attempt_id = self.store.start_attempt(operation_id, attempt_no, op.normalized_intent)
            self.store.audit(operation_id, EventType.EXECUTE_ATTEMPTED, {"attempt_no": attempt_no})
            self.store.conn.execute("COMMIT")
            return self.store.get_by_id(operation_id), attempt_id
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

    def run_to_completion(self, operation_id: int) -> Operation:
        deadline = self.clock.now() + self.deadline_seconds
        for _ in range(self.max_attempts):
            if self.clock.now() > deadline:
                break
            op, attempt_id = self.revalidate_and_start_attempt(operation_id)
            if attempt_id is None:
                return op  # denied/expired/already-terminal — nothing to execute

            outcome = self.downstream.call(op.principal, op.tool, op.repo, op.branch, op.normalized_intent)

            if outcome is None:
                self.store.finish_attempt(attempt_id, "deferred")
                self.store.audit(operation_id, EventType.EFFECT_DEFERRED, {})
                self.store.cas(operation_id, (State.EXECUTING,), State.APPROVED)
                continue

            if outcome == Outcome.FAILED:
                self.store.finish_attempt(attempt_id, Outcome.FAILED.value)
                self.store.audit(operation_id, EventType.EFFECT_FAILED_PERMANENT, {})
                self.store.cas(operation_id, (State.EXECUTING,), State.FAILED)
                self.store.audit(operation_id, EventType.OPERATION_FAILED, {})
                return self.store.get_by_id(operation_id)

            if outcome == Outcome.LOST:
                self.store.finish_attempt(attempt_id, Outcome.LOST.value)
                self.store.audit(operation_id, EventType.EFFECT_LOST_RESPONSE, {})
                self.store.cas(operation_id, (State.EXECUTING,), State.APPROVED)
                continue

            self.store.finish_attempt(attempt_id, Outcome.SUCCESS.value)
            self.store.audit(operation_id, EventType.EFFECT_APPLIED, {})
            self.store.cas(operation_id, (State.EXECUTING,), State.SUCCEEDED)
            self.store.audit(operation_id, EventType.OPERATION_SUCCEEDED, {})
            return self.store.get_by_id(operation_id)

        self.store.cas(operation_id, (State.EXECUTING, State.APPROVED), State.FAILED)
        self.store.audit(operation_id, EventType.OPERATION_FAILED, {"reason": "budget_exhausted"})
        return self.store.get_by_id(operation_id)

    def submit_and_run(self, request: Request, auto_approve: bool = True) -> Operation:
        try:
            op = self.submit(request)
        except ConflictError as exc:
            return self.store.get_by_id(exc.existing_operation_id)
        if op.state != State.PENDING_APPROVAL:
            return op
        if auto_approve:
            op = self.approve(op.id)
        if op.state != State.APPROVED:
            return op
        return self.run_to_completion(op.id)
