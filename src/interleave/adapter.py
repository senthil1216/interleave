from __future__ import annotations

from interleave.models import Outcome
from interleave.store import OperationStore

FAULTS = ("none", "503", "lost-response", "permanent-failure")

NEAR_MISS_WINDOW_SECONDS = 5.0


class FakeDownstreamAdapter:
    """Simulates a GitHub-style PR-creation API. Fault fires once, on this instance's
    first call, then self-heals — matching 'success | 503-then-success | lost-response |
    permanent failure'. Idempotency is keyed by whatever the caller passes: the gateway
    passes normalized_intent (stable across coalesced retries), the naive baseline passes
    a content hash (different per differently-worded attempt) — same adapter code, the
    difference in outcome comes entirely from what identity the caller supplies."""

    def __init__(self, store: OperationStore, fault: str = "none"):
        if fault not in FAULTS:
            raise ValueError(f"unknown fault: {fault}")
        self.store = store
        self.fault = fault
        self._calls = 0

    def call(self, principal: str, tool: str, repo: str, branch: str, key: str) -> Outcome | None:
        if self.store.ledger_has(principal, tool, key):
            return Outcome.SUCCESS

        self._calls += 1
        first_call = self._calls == 1

        if self.fault == "permanent-failure":
            return Outcome.FAILED

        if self.fault == "503" and first_call:
            return None  # deferred: nothing applied, caller should retry

        if self.fault == "lost-response" and first_call:
            self.store.ledger_apply(principal, tool, repo, branch, key)
            self._check_near_miss(principal, repo, branch, key)
            return Outcome.LOST  # applied, but the ack never made it back

        self.store.ledger_apply(principal, tool, repo, branch, key)
        self._check_near_miss(principal, repo, branch, key)
        return Outcome.SUCCESS

    def _check_near_miss(self, principal: str, repo: str, branch: str, key: str) -> None:
        from interleave.models import EventType

        if self.store.near_miss(principal, repo, branch, key, NEAR_MISS_WINDOW_SECONDS):
            self.store.audit(
                None,
                EventType.NEAR_MISS_ALARMED,
                {"principal": principal, "repo": repo, "branch": branch, "key": key},
            )
