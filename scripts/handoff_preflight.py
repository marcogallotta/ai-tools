"""Fail-closed executability preflight for Dish agent handoffs.

The preflight validates an already-prepared handoff.  It does not create tasks, grant
standing role authority, or authorize any prerequisite mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping


TASK_GID_RE = re.compile(r"(?<!\d)\d{16}(?!\d)")
UNRESOLVED_TOKEN_RE = re.compile(
    r"<[^>\n]+>|\{\{[^}\n]+\}\}|\$\{[^}\n]+\}|(?i:\b(?:PLACEHOLDER|TBD|TODO)\b)"
)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
INLINE_MAX_NONEMPTY_LINES = 8
INLINE_MAX_CHARS = 700


class HandoffReadiness(str, Enum):
    EXECUTABLE = "executable"
    PREPARATION_REQUIRED = "draft_preparation_required"
    ROUTING_REQUIRED = "routing_required"
    INVALID = "invalid"


class HandoffHost(str, Enum):
    CHATGPT = "chatgpt"
    LOCAL = "local"


class HandoffPresentationKind(str, Enum):
    NONE = "no_manual_relay"
    INLINE = "inline_copy_block"
    LOCAL_FILE = "local_temp_file"
    CHATGPT_ARTIFACT = "chatgpt_artifact"
    BLOCKED = "transport_capability_blocked"


@dataclass(frozen=True)
class HandoffPreflight:
    readiness: HandoffReadiness
    reason: str
    next_action: str | None = None

    @property
    def executable(self) -> bool:
        return self.readiness is HandoffReadiness.EXECUTABLE


@dataclass(frozen=True)
class HandoffPresentation:
    kind: HandoffPresentationKind
    copy_block: str | None
    reason: str
    file_path: Path | None = None


def _copy_block(value: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{value}\n{fence}"


def _nonempty_line_count(value: str) -> int:
    return sum(bool(line.strip()) for line in value.splitlines())


def prepare_handoff_presentation(
    *,
    payload: str,
    host: HandoffHost,
    manual_relay_required: bool,
    reconstructable_locator: str | None = None,
    chatgpt_artifact_locator: str | None = None,
    temp_directory: Path = Path("/tmp"),
) -> HandoffPresentation:
    """Render one complete manual relay without changing handoff authority.

    Ordinary reconstructable work uses its locator. Non-reconstructable payloads are
    inline only at or below both limits; larger local payloads are written exactly
    once to a private temporary file, while larger ChatGPT payloads require a
    supported transferable artifact.
    """
    host = HandoffHost(host)
    if not manual_relay_required:
        return HandoffPresentation(
            HandoffPresentationKind.NONE,
            None,
            "no manual relay is required",
        )

    if reconstructable_locator is not None:
        locator = reconstructable_locator.strip()
        if not locator:
            raise ValueError("reconstructable locator must not be blank")
        return HandoffPresentation(
            HandoffPresentationKind.INLINE,
            _copy_block(locator),
            "receiver can reconstruct current context from the locator",
        )

    inline = (
        _nonempty_line_count(payload) <= INLINE_MAX_NONEMPTY_LINES
        and len(payload) <= INLINE_MAX_CHARS
    )
    if inline:
        return HandoffPresentation(
            HandoffPresentationKind.INLINE,
            _copy_block(payload),
            "non-reconstructable payload fits both inline limits",
        )

    if host is HandoffHost.CHATGPT:
        if chatgpt_artifact_locator and chatgpt_artifact_locator.strip():
            locator = chatgpt_artifact_locator.strip()
            return HandoffPresentation(
                HandoffPresentationKind.CHATGPT_ARTIFACT,
                _copy_block(locator),
                "complete payload is available through a supported transferable artifact",
            )
        return HandoffPresentation(
            HandoffPresentationKind.BLOCKED,
            None,
            "ChatGPT cannot transfer the complete non-reconstructable payload through a supported artifact",
        )

    directory = Path(temp_directory).resolve()
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("temporary handoff directory must be an existing absolute directory")
    descriptor, raw_path = tempfile.mkstemp(prefix="dish-handoff-", suffix=".txt", dir=directory)
    path = Path(raw_path).resolve()
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(payload)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return HandoffPresentation(
        HandoffPresentationKind.LOCAL_FILE,
        _copy_block(str(path)),
        "complete payload was written to the exact local path",
        path,
    )


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
