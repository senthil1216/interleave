from __future__ import annotations

from interleave.models import EventType, State
from interleave.store import OperationStore


class ApprovalService:
    def __init__(self, store: OperationStore, clock, ttl_seconds: float = 900.0):
        self.store = store
        self.clock = clock
        self.ttl_seconds = ttl_seconds

    def approve(self, operation_id: int) -> bool:
        """Accepts PENDING_APPROVAL (the ordinary case) and EXPIRED (a fresh ask after
        TTL lapsed) — but never DENIED. Expiry is recoverable by construction; an explicit
        human 'no' (revoke or policy deny) is not something a later approve() call should
        be able to silently override."""
        op = self.store.get_by_id(operation_id)
        now = self.clock.now()
        was_expired = op.state == State.EXPIRED
        ok = self.store.cas(
            operation_id,
            (State.PENDING_APPROVAL, State.EXPIRED),
            State.APPROVED,
            approved_at=now,
            expires_at=now + self.ttl_seconds,
            approval_epoch=op.approval_epoch + 1,
        )
        if ok:
            self.store.audit(
                operation_id,
                EventType.APPROVAL_GRANTED,
                {"epoch": op.approval_epoch + 1, "fresh_authority": was_expired},
            )
        return ok

    def deny(self, operation_id: int) -> bool:
        ok = self.store.cas(operation_id, (State.PENDING_APPROVAL,), State.DENIED)
        if ok:
            self.store.audit(operation_id, EventType.OPERATION_DENIED, {"reason": "human_denied"})
        return ok

    def revoke(self, operation_id: int) -> bool:
        """Approve -> revoke -> retry is denied: revoking moves straight to the terminal
        `denied` state, so a retry that lands after this sees a non-approved state and
        execute() bails out before ever calling the downstream."""
        ok = self.store.cas(operation_id, (State.APPROVED,), State.DENIED)
        if ok:
            self.store.audit(operation_id, EventType.APPROVAL_REVOKED, {})
            self.store.audit(operation_id, EventType.OPERATION_DENIED, {"reason": "revoked"})
        return ok
