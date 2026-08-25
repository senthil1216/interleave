from __future__ import annotations

import json

from interleave.store import OperationStore


def render_terminal(store: OperationStore, operation_id: int | None = None) -> str:
    """Timestamps are relative to the first event in this trail, not raw clock.now() --
    RealClock uses time.monotonic(), whose epoch is arbitrary and unreadable on its own."""
    rows = store.audit_trail(operation_id)
    lines = []
    t0 = rows[0][1] if rows else 0.0
    for op_id, ts, event_type, detail_json in rows:
        detail = json.loads(detail_json)
        op_part = f"op={op_id}" if op_id is not None else "op=-"
        detail_part = f" {detail}" if detail else ""
        lines.append(f"[+{ts - t0:7.3f}s] {op_part:8} {event_type}{detail_part}")
    return "\n".join(lines)
