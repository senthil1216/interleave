from __future__ import annotations

from interleave.models import EventType, State
from interleave.store import OperationStore


def reconcile(store: OperationStore) -> list[int]:
    """Runs at gateway construction, every invocation. Reads attempts with no recorded
    outcome (the process died between writing the intent record and finishing the call)
    and the ledger, and adopts whatever already applied instead of re-executing it — this
    is what makes 'at most one side effect' hold across a crash, not just within one
    process run. `--fault crash` (CLI) triggers a real os._exit(1); the equivalent in
    tests is dropping the in-memory Gateway object with no teardown and constructing a
    fresh one against the same file — durable state makes that the real test."""
    adopted: list[int] = []
    for attempt_id, operation_id, key in store.orphaned_attempts():
        op = store.get_by_id(operation_id)
        if store.ledger_has(op.principal, op.tool, key):
            store.finish_attempt(attempt_id, "adopted")
            store.cas(operation_id, (State.EXECUTING,), State.SUCCEEDED)
            store.audit(operation_id, EventType.RECONCILED_ORPHAN, {"attempt_id": attempt_id, "resolution": "adopted"})
            store.audit(operation_id, EventType.OPERATION_SUCCEEDED, {"via": "reconcile"})
            adopted.append(operation_id)
        else:
            store.finish_attempt(attempt_id, "orphaned_not_applied")
            store.cas(operation_id, (State.EXECUTING,), State.APPROVED)
            store.audit(operation_id, EventType.RECONCILED_ORPHAN, {"attempt_id": attempt_id, "resolution": "retryable"})
    return adopted
