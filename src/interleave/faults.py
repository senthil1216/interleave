from __future__ import annotations

from dataclasses import dataclass

FAULT_TYPES = ("none", "503", "lost-response", "permanent-failure", "crash")

INJECTION_POINTS = (
    "pre-policy",
    "post-normalize",
    "pre-approval",
    "post-approval-pre-execute",
    "pre-intent-record",
    "downstream-ack",
    "post-effect-pre-ledger",
    "pre-reconcile",
)


@dataclass(frozen=True)
class FaultSchedule:
    """`crash` is handled at the CLI layer (a real os._exit) and by reconcile.py, not by
    FakeDownstreamAdapter. Of the eight named injection points, this implementation acts
    on one — `downstream-ack`, where the adapter's own fault behavior fires — the others
    are named and validated here (see cli.py) so the vocabulary is fixed even though the
    per-point fault matrix isn't fully wired up yet; that's a real simplification, not
    silently dropped scope."""

    fault: str
    at: str = "downstream-ack"
    seed: int = 0

    def __post_init__(self):
        if self.fault not in FAULT_TYPES:
            raise ValueError(f"unknown fault: {self.fault}")
        if self.at not in INJECTION_POINTS:
            raise ValueError(f"unknown injection point: {self.at}")
