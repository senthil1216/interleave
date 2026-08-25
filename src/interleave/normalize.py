from __future__ import annotations

from interleave.models import Request


def normalize(request: Request) -> str:
    """Identity is (principal, tool, normalized_intent); principal and tool are separate
    columns, so this only has to fold repo/branch. Title/body wording is deliberately
    excluded — that's the whole point of claim 1."""
    repo = request.repo.strip().lower()
    branch = request.branch.strip().lower()
    return f"{repo}:{branch}"
