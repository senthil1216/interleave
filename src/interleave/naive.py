from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from interleave.adapter import FakeDownstreamAdapter
from interleave.models import EventType, Outcome, Request
from interleave.store import OperationStore

NAIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS naive_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def content_hash_key(request: Request) -> str:
    """Deliberately hashes the *entire* payload, wording included — this is the naive
    fallback a sloppy client falls back on, and it's exactly what fails to coalesce a
    resampled retry."""
    canonical = json.dumps(request.to_payload(), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass
class NaiveResult:
    prompted: bool
    applied: bool
    operation_id: int


class NaiveBaseline:
    """Same interface, deliberately unguarded: content-hash keying (no coalescing across
    wording variants), no transaction (approval is a payload flag, not live-dereferenced
    state), no ledger (no idempotency protection at all), and no retry-outcome handling —
    whatever the first downstream call returns is treated as final, so an 'unknown outcome'
    (lost-response) is trusted as success. This is what the invariants have something real
    to fail against."""

    def __init__(self, conn: sqlite3.Connection, clock, downstream: FakeDownstreamAdapter):
        self.conn = conn
        self.clock = clock
        self.downstream = downstream
        # Reuses OperationStore purely for its audit_log table/methods (already present in
        # this connection's schema) so the naive timeline uses the *same* event vocabulary
        # as the gateway's -- same names, side by side, so the divergence (extra
        # operation_created instead of operation_joined, no revalidation step, etc.) is
        # visible event-by-event instead of only in a one-line summary.
        self.store = OperationStore(conn, clock)
        conn.executescript(NAIVE_SCHEMA)

    def submit_and_run(self, request: Request, auto_approve: bool = True) -> NaiveResult:
        key = content_hash_key(request)
        now = self.clock.now()
        row = self.conn.execute(
            "SELECT id FROM naive_operations WHERE content_hash=?", (key,)
        ).fetchone()
        if row is not None:
            return NaiveResult(prompted=False, applied=False, operation_id=row[0])

        cur = self.conn.execute(
            "INSERT INTO naive_operations (content_hash, payload_json, approved, state, created_at) "
            "VALUES (?, ?, 0, 'pending_approval', ?)",
            (key, json.dumps(request.to_payload()), now),
        )
        op_id = cur.lastrowid
        self.store.audit(op_id, EventType.OPERATION_CREATED, {"principal": request.principal})
        prompted = True
        self.store.audit(op_id, EventType.APPROVAL_REQUESTED, {})

        if not auto_approve:
            self.store.audit(op_id, EventType.OPERATION_DENIED, {"reason": "human_denied"})
            return NaiveResult(prompted=prompted, applied=False, operation_id=op_id)

        self.conn.execute(
            "UPDATE naive_operations SET approved=1, state='approved' WHERE id=?", (op_id,)
        )
        self.store.audit(op_id, EventType.APPROVAL_GRANTED, {})

        # "Approval trusted from payload": re-reads the row instead of dereferencing any
        # live state, so nothing here would notice a revoke that happened in between --
        # and notably, there's no revalidation event here at all, unlike the gateway's
        # execute_attempted step, which re-checks live state right before acting.
        approved_flag = self.conn.execute(
            "SELECT approved FROM naive_operations WHERE id=?", (op_id,)
        ).fetchone()[0]
        if not approved_flag:
            return NaiveResult(prompted=prompted, applied=False, operation_id=op_id)

        self.store.audit(op_id, EventType.EXECUTE_ATTEMPTED, {"attempt_no": 1})
        outcome = self.downstream.call(request.principal, request.tool, request.repo, request.branch, key)
        applied = outcome in (Outcome.SUCCESS, Outcome.LOST)  # no distinction — the claim-3 bug
        if outcome == Outcome.LOST:
            self.store.audit(op_id, EventType.EFFECT_LOST_RESPONSE, {})
        elif outcome == Outcome.SUCCESS:
            self.store.audit(op_id, EventType.EFFECT_APPLIED, {})
        elif outcome == Outcome.FAILED:
            self.store.audit(op_id, EventType.EFFECT_FAILED_PERMANENT, {})
        # outcome is None on a 503-defer; naive never retries, so that's a dead end too --
        # recorded as a failure below, since nothing will ever resolve it.
        state = "succeeded" if applied else "failed"
        self.conn.execute("UPDATE naive_operations SET state=? WHERE id=?", (state, op_id))
        self.store.audit(
            op_id,
            EventType.OPERATION_SUCCEEDED if applied else EventType.OPERATION_FAILED,
            {},
        )
        return NaiveResult(prompted=prompted, applied=applied, operation_id=op_id)

    def prompt_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM naive_operations").fetchone()[0]
