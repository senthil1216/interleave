from __future__ import annotations

from typing import Protocol

from interleave.models import Operation


class Policy(Protocol):
    def check(self, op: Operation) -> bool: ...


class AllowAll:
    def check(self, op: Operation) -> bool:
        return True


class DenyAlways:
    def check(self, op: Operation) -> bool:
        return False


class DenyAfter:
    """Flips from allow to deny once the clock passes deny_at — the fixture for
    'policy revalidated live, not just approval': approve while allowed, then let
    the clock cross deny_at before execution reaches revalidation."""

    def __init__(self, clock, deny_at: float):
        self.clock = clock
        self.deny_at = deny_at

    def check(self, op: Operation) -> bool:
        return self.clock.now() < self.deny_at
