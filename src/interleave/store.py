from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from interleave.models import EventType, Operation, Request, State

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,
    tool TEXT NOT NULL,
    normalized_intent TEXT NOT NULL,
    canonical_payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    approval_epoch INTEGER NOT NULL DEFAULT 0,
    approved_at REAL,
    expires_at REAL,
    UNIQUE(principal, tool, normalized_intent)
);

CREATE TABLE IF NOT EXISTS variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    payload_json TEXT NOT NULL,
    variant_no INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id INTEGER NOT NULL REFERENCES operations(id),
    attempt_no INTEGER NOT NULL,
    intent_hash TEXT NOT NULL,
    started_at REAL NOT NULL,
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,
    tool TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    normalized_intent TEXT NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id INTEGER,
    ts REAL NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


class ConflictError(Exception):
    def __init__(self, existing_operation_id: int):
        self.existing_operation_id = existing_operation_id
        super().__init__(f"conflict with operation {existing_operation_id}")


@dataclass
class GetOrCreateResult:
    operation: Operation
    created: bool


def _row_to_operation(row) -> Operation:
    return Operation(
        id=row[0],
        principal=row[1],
        tool=row[2],
        normalized_intent=row[3],
        canonical_payload=json.loads(row[4]),
        state=State(row[5]),
        created_at=row[6],
        approval_epoch=row[7],
        approved_at=row[8],
        expires_at=row[9],
    )


_OP_COLUMNS = (
    "id, principal, tool, normalized_intent, canonical_payload_json, state, "
    "created_at, approval_epoch, approved_at, expires_at"
)


class OperationStore:
    """No process mutex anywhere — concurrency safety comes entirely from SQLite's
    UNIQUE constraint (coalescing) and CAS-style conditional UPDATEs (state transitions),
    so the tests that rely on those are exercising the real thing, not a lock around it."""

    def __init__(self, conn: sqlite3.Connection, clock):
        self.conn = conn
        self.clock = clock

    def audit(self, operation_id: int | None, event_type: EventType, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (operation_id, ts, event_type, detail_json) VALUES (?, ?, ?, ?)",
            (operation_id, self.clock.now(), event_type.value, json.dumps(detail)),
        )

    def get_by_id(self, operation_id: int) -> Operation:
        row = self.conn.execute(
            f"SELECT {_OP_COLUMNS} FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return _row_to_operation(row)

    def get_or_create(self, request: Request, normalized_intent: str) -> GetOrCreateResult:
        now = self.clock.now()
        payload_json = json.dumps(request.to_payload())
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                "INSERT INTO operations (principal, tool, normalized_intent, "
                "canonical_payload_json, state, created_at, approval_epoch) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (
                    request.principal,
                    request.tool,
                    normalized_intent,
                    payload_json,
                    State.PENDING_APPROVAL.value,
                    now,
                ),
            )
            op = self.get_by_id(cur.lastrowid)
            self.audit(op.id, EventType.OPERATION_CREATED, {"principal": request.principal})
            self.conn.execute("COMMIT")
            return GetOrCreateResult(operation=op, created=True)
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK")
            return self._join_or_conflict(request, normalized_intent, payload_json, now)

    def _join_or_conflict(self, request, normalized_intent, payload_json, now) -> GetOrCreateResult:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                f"SELECT {_OP_COLUMNS} FROM operations "
                "WHERE principal=? AND tool=? AND normalized_intent=?",
                (request.principal, request.tool, normalized_intent),
            ).fetchone()
            existing = _row_to_operation(row)
            if existing.target_ref != request.target_ref:
                self.audit(
                    existing.id,
                    EventType.CONFLICT_CREATED,
                    {"attempted_target_ref": request.target_ref, "canonical_target_ref": existing.target_ref},
                )
                self.conn.execute("COMMIT")
                raise ConflictError(existing.id)
            variant_no = (
                1
                + self.conn.execute(
                    "SELECT COUNT(*) FROM variants WHERE operation_id=?", (existing.id,)
                ).fetchone()[0]
            )
            self.conn.execute(
                "INSERT INTO variants (operation_id, payload_json, variant_no, created_at) "
                "VALUES (?, ?, ?, ?)",
                (existing.id, payload_json, variant_no, now),
            )
            # Deliberately named variant_no, not attempt_no: this counts alternate wordings
            # recorded for the operation, a different axis from attempts.attempt_no (which
            # counts execution retries against the downstream) -- reusing "attempt_no" for
            # both made the timeline read like the same counter when it isn't.
            self.audit(existing.id, EventType.OPERATION_JOINED, {"variant_no": variant_no})
            self.audit(existing.id, EventType.VARIANT_RECORDED, {"variant_no": variant_no})
            self.conn.execute("COMMIT")
            return GetOrCreateResult(operation=existing, created=False)
        except ConflictError:
            raise
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def cas(self, operation_id: int, expected_states: tuple[State, ...], new_state: State, **extra) -> bool:
        set_clauses = ["state=?"]
        params: list = [new_state.value]
        for k, v in extra.items():
            set_clauses.append(f"{k}=?")
            params.append(v)
        placeholders = ",".join("?" * len(expected_states))
        params.append(operation_id)
        params.extend(s.value for s in expected_states)
        sql = f"UPDATE operations SET {', '.join(set_clauses)} WHERE id=? AND state IN ({placeholders})"
        cur = self.conn.execute(sql, params)
        return cur.rowcount == 1

    def next_attempt_no(self, operation_id: int) -> int:
        return (
            1
            + self.conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE operation_id=?", (operation_id,)
            ).fetchone()[0]
        )

    def start_attempt(self, operation_id: int, attempt_no: int, intent_hash: str) -> int:
        now = self.clock.now()
        cur = self.conn.execute(
            "INSERT INTO attempts (operation_id, attempt_no, intent_hash, started_at, outcome) "
            "VALUES (?, ?, ?, ?, NULL)",
            (operation_id, attempt_no, intent_hash, now),
        )
        return cur.lastrowid

    def finish_attempt(self, attempt_id: int, outcome: str) -> None:
        self.conn.execute("UPDATE attempts SET outcome=? WHERE id=?", (outcome, attempt_id))

    def orphaned_attempts(self) -> list[tuple[int, int, str]]:
        rows = self.conn.execute(
            "SELECT id, operation_id, intent_hash FROM attempts WHERE outcome IS NULL"
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def ledger_has(self, principal: str, tool: str, normalized_intent: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM ledger WHERE principal=? AND tool=? AND normalized_intent=? LIMIT 1",
            (principal, tool, normalized_intent),
        ).fetchone()
        return row is not None

    def ledger_apply(self, principal: str, tool: str, repo: str, branch: str, normalized_intent: str) -> None:
        now = self.clock.now()
        self.conn.execute(
            "INSERT INTO ledger (principal, tool, repo, branch, normalized_intent, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (principal, tool, repo, branch, normalized_intent, now),
        )

    def near_miss(self, principal: str, repo: str, branch: str, normalized_intent: str, window_seconds: float) -> bool:
        """Detection only, never blocking — it can't tell an envelope-violating duplicate
        from a legitimate second operation, so it surfaces, it doesn't prevent."""
        now = self.clock.now()
        rows = self.conn.execute(
            "SELECT normalized_intent, applied_at FROM ledger "
            "WHERE principal=? AND repo=? AND branch=? AND applied_at >= ?",
            (principal, repo, branch, now - window_seconds),
        ).fetchall()
        return any(r[0] != normalized_intent for r in rows)

    def audit_trail(self, operation_id: int | None = None) -> list[tuple]:
        if operation_id is None:
            return self.conn.execute(
                "SELECT operation_id, ts, event_type, detail_json FROM audit_log ORDER BY id"
            ).fetchall()
        return self.conn.execute(
            "SELECT operation_id, ts, event_type, detail_json FROM audit_log "
            "WHERE operation_id=? ORDER BY id",
            (operation_id,),
        ).fetchall()
