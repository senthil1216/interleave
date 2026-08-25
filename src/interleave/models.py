from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class State(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    EXPIRED = "expired"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """EXPIRED is deliberately excluded: a lapsed TTL is recoverable via a fresh
        approve() call (see ApprovalService.approve), unlike SUCCEEDED/DENIED/FAILED,
        which the terminal-states-immutable invariant actually covers."""
        return self in (State.SUCCEEDED, State.DENIED, State.FAILED)


class EventType(str, Enum):
    POLICY_DENIED_INTAKE = "policy_denied_intake"
    OPERATION_CREATED = "operation_created"
    OPERATION_JOINED = "operation_joined"
    VARIANT_RECORDED = "variant_recorded"
    CONFLICT_CREATED = "conflict_created"
    NEAR_MISS_ALARMED = "near_miss_alarmed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REVOKED = "approval_revoked"
    APPROVAL_EXPIRED = "approval_expired"
    POLICY_DENIED_REVALIDATION = "policy_denied_revalidation"
    EXECUTE_ATTEMPTED = "execute_attempted"
    EFFECT_DEFERRED = "effect_deferred"
    EFFECT_APPLIED = "effect_applied"
    EFFECT_LOST_RESPONSE = "effect_lost_response"
    EFFECT_FAILED_PERMANENT = "effect_failed_permanent"
    OPERATION_SUCCEEDED = "operation_succeeded"
    OPERATION_DENIED = "operation_denied"
    OPERATION_EXPIRED = "operation_expired"
    OPERATION_FAILED = "operation_failed"
    RECONCILED_ORPHAN = "reconciled_orphan"


class Outcome(str, Enum):
    SUCCESS = "success"
    LOST = "lost"
    FAILED = "failed"


@dataclass(frozen=True)
class Request:
    principal: str
    tool: str
    repo: str
    branch: str
    title: str
    body: str
    target_ref: str = "base-sha-1"

    def to_payload(self) -> dict:
        return {
            "principal": self.principal,
            "tool": self.tool,
            "repo": self.repo,
            "branch": self.branch,
            "title": self.title,
            "body": self.body,
            "target_ref": self.target_ref,
        }


@dataclass
class Operation:
    id: int
    principal: str
    tool: str
    normalized_intent: str
    canonical_payload: dict
    state: State
    created_at: float
    approval_epoch: int
    approved_at: float | None
    expires_at: float | None

    @property
    def target_ref(self) -> str:
        return self.canonical_payload["target_ref"]

    @property
    def repo(self) -> str:
        return self.canonical_payload["repo"]

    @property
    def branch(self) -> str:
        return self.canonical_payload["branch"]
