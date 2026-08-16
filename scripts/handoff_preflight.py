"""Fail-closed executability preflight for Dish agent handoffs.

The preflight validates an already-prepared handoff.  It does not create tasks, grant
standing role authority, or authorize any prerequisite mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Mapping


TASK_GID_RE = re.compile(r"(?<!\d)\d{16}(?!\d)")
UNRESOLVED_TOKEN_RE = re.compile(
    r"<[^>\n]+>|\{\{[^}\n]+\}\}|\$\{[^}\n]+\}|(?i:\b(?:PLACEHOLDER|TBD|TODO)\b)"
)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


class HandoffReadiness(str, Enum):
    EXECUTABLE = "executable"
    PREPARATION_REQUIRED = "draft_preparation_required"
    ROUTING_REQUIRED = "routing_required"
    INVALID = "invalid"


@dataclass(frozen=True)
class HandoffPreflight:
    readiness: HandoffReadiness
    reason: str
    next_action: str | None = None

    @property
    def executable(self) -> bool:
        return self.readiness is HandoffReadiness.EXECUTABLE


def validate_handoff(
    *,
    text: str,
    required_role: str,
    destination_role: str | None,
    required_task_gid: str | None = None,
    task_readback_gid: str | None = None,
    required_baseline: str | None = None,
    baseline_readback: str | None = None,
    required_identities: Mapping[str, str | None] | None = None,
    prerequisite_mutation: str | None = None,
    prerequisite_mutation_authorized: bool = False,
) -> HandoffPreflight:
    """Return executable only when every mandatory precondition is resolved/read back."""
    token = UNRESOLVED_TOKEN_RE.search(text)
    if token:
        return HandoffPreflight(
            HandoffReadiness.INVALID,
            f"unresolved handoff token: {token.group(0)}",
            "resolve the placeholder before presenting the handoff",
        )

    if prerequisite_mutation and not prerequisite_mutation_authorized:
        return HandoffPreflight(
            HandoffReadiness.PREPARATION_REQUIRED,
            f"required prerequisite write is not authorized: {prerequisite_mutation}",
            prerequisite_mutation,
        )

    if required_task_gid is not None:
        if not re.fullmatch(r"\d{16}", required_task_gid):
            return HandoffPreflight(HandoffReadiness.INVALID, "required task identity is malformed")
        if required_task_gid not in TASK_GID_RE.findall(text):
            return HandoffPreflight(
                HandoffReadiness.INVALID,
                "handoff does not contain the required owning task identity",
            )
        if task_readback_gid != required_task_gid:
            return HandoffPreflight(
                HandoffReadiness.INVALID,
                "owning task identity did not read back exactly",
            )

    if required_baseline is not None:
        if not FULL_SHA_RE.fullmatch(required_baseline):
            return HandoffPreflight(HandoffReadiness.INVALID, "required baseline is not a full SHA")
        if required_baseline not in text:
            return HandoffPreflight(
                HandoffReadiness.INVALID,
                "handoff does not bind the required exact baseline",
            )
        if baseline_readback != required_baseline:
            return HandoffPreflight(
                HandoffReadiness.INVALID,
                "exact baseline identity did not read back",
            )

    for label, observed in (required_identities or {}).items():
        if observed is None or not str(observed).strip():
            return HandoffPreflight(
                HandoffReadiness.INVALID,
                f"required durable identity is unresolved: {label}",
            )
        if str(observed) not in text:
            return HandoffPreflight(
                HandoffReadiness.INVALID,
                f"handoff does not contain required durable identity: {label}",
            )

    if destination_role is None:
        return HandoffPreflight(
            HandoffReadiness.ROUTING_REQUIRED,
            f"destination standing role is not verified; required role is {required_role}",
            f"send only to a {required_role} Project/session",
        )
    if destination_role.casefold() != required_role.casefold():
        return HandoffPreflight(
            HandoffReadiness.ROUTING_REQUIRED,
            f"known destination role {destination_role!r} is incompatible with required {required_role!r}",
            f"send only to a {required_role} Project/session",
        )

    return HandoffPreflight(HandoffReadiness.EXECUTABLE, "all handoff prerequisites verified")


def require_distinct_task_identities(*task_gids: str) -> None:
    if any(not re.fullmatch(r"\d{16}", gid) for gid in task_gids):
        raise ValueError("handoff task identity is malformed")
    if len(set(task_gids)) != len(task_gids):
        raise ValueError("independent handoffs require distinct fresh task identities")
