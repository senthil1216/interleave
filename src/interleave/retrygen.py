from __future__ import annotations

import random
import uuid
from dataclasses import replace

from interleave.models import Request

MODES = ("wording-variant", "content-identical", "lost-uuid")


def generate_attempts(mode: str, base: Request, rng: random.Random, n: int = 2) -> list[Request]:
    """Simulates how an agent's retry actually arrives, not how a well-behaved client
    would. 'wording-variant' is the shape every mode ultimately produces at the Request
    level — a resampled agent rewrites title/body on every attempt. 'lost-uuid' and
    'content-identical' exist as named modes because they're the *comparison baselines*
    used in the claim-1 test (see lost_uuid_key/naive.content_hash_key), not because the
    Request shape differs; the interesting part is which key scheme coalesces the same
    two requests, not how the requests themselves are built."""
    if n < 1:
        raise ValueError("n must be >= 1")
    if mode not in MODES:
        raise ValueError(f"unknown retry mode: {mode}")
    if mode == "content-identical":
        return [base for _ in range(n)]
    attempts = [base]
    for i in range(1, n):
        suffix = f" (attempt {i + 1}, {rng.randint(1000, 9999)})"
        attempts.append(replace(base, title=base.title + suffix, body=base.body + suffix))
    return attempts


def lost_uuid_key(_: Request) -> str:
    """Primary claim-1 baseline: a caller that mints a UUID once and loses it on
    resample — modeled as always minting a *fresh* UUID, since from the gateway's
    perspective a lost ID and a never-reused one are indistinguishable. Guaranteed
    never to coalesce, by construction."""
    return str(uuid.uuid4())
