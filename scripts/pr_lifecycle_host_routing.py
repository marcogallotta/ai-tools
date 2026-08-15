"""Deterministic remote-first classification for PR lifecycle residual work."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

CHATGPT_IMPLEMENTATION = "CHATGPT_IMPLEMENTATION"
LOCAL_IMPLEMENTATION = "LOCAL_IMPLEMENTATION"
TESTS_ONLY = "TESTS ONLY"
IMPLEMENTATION_PUBLICATION = "IMPLEMENTATION / PUBLICATION"
LOCAL_SYSTEM_ACCESS = "LOCAL SYSTEM ACCESS"

_IMPLEMENTATION_BOUNDARY_RE = re.compile(
    r"^IMPLEMENTATION\s*/\s*PUBLICATION\s*[—-]\s*(?P<capability>.+?);\s*"
    r"fallbacks exhausted:\s*(?P<fallbacks>.+?)\s*$",
    re.IGNORECASE,
)
_SYSTEM_BOUNDARY_RE = re.compile(
    r"^LOCAL SYSTEM ACCESS\s*[—-]\s*(?P<scope>.+?)\s*$", re.IGNORECASE
)
_TEST_BOUNDARY_RE = re.compile(r"^TESTS ONLY\s*[—-]\s*(?P<scope>.+?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class LocalWorkBoundary:
    work_type: str
    scope: str
    unavailable_remote_capability: str | None = None
    fallbacks_exhausted: tuple[str, ...] = ()
    runtime: str | None = None

    @property
    def local_implementation_eligible(self) -> bool:
        return bool(
            self.work_type == IMPLEMENTATION_PUBLICATION
            and self.unavailable_remote_capability
            and self.fallbacks_exhausted
        )


def _fallbacks(value: str) -> tuple[str, ...]:
    raw = [item.strip() for item in re.split(r"\s*[,|]\s*", value) if item.strip()]
    return tuple(raw)


def classify_requirement(text: str, *, default_kind: str | None = None) -> LocalWorkBoundary:
    """Classify one durable local requirement without using runtime as a scope proxy."""
    value = str(text or "").strip()
    match = _IMPLEMENTATION_BOUNDARY_RE.fullmatch(value)
    if match:
        return LocalWorkBoundary(
            work_type=IMPLEMENTATION_PUBLICATION,
            scope=value,
            unavailable_remote_capability=match.group("capability").strip(),
            fallbacks_exhausted=_fallbacks(match.group("fallbacks")),
        )
    match = _TEST_BOUNDARY_RE.fullmatch(value)
    if match:
        return LocalWorkBoundary(TESTS_ONLY, match.group("scope").strip())
    match = _SYSTEM_BOUNDARY_RE.fullmatch(value)
    if match:
        return LocalWorkBoundary(LOCAL_SYSTEM_ACCESS, match.group("scope").strip())

    if default_kind == "certification":
        return LocalWorkBoundary(TESTS_ONLY, value)
    if default_kind == "system_access":
        return LocalWorkBoundary(LOCAL_SYSTEM_ACCESS, value)
    if default_kind == "implementation":
        # Presentation may still name the work type, but missing exact capability/fallback
        # proof is deliberately not enough to route substantive source work locally.
        return LocalWorkBoundary(IMPLEMENTATION_PUBLICATION, value)
    raise ValueError(f"unclassified local-work requirement: {value!r}")


def classify_local_work_item(item: Mapping[str, Any]) -> LocalWorkBoundary:
    return classify_requirement(
        str(item.get("instruction") or "complete the exact PR-local handoff"),
        default_kind=str(item.get("kind") or "") or None,
    )


def implementation_host_for_boundary(boundary: LocalWorkBoundary | None) -> str:
    """Remote-first: local Implementation requires explicit class-B proof."""
    if boundary is not None and boundary.local_implementation_eligible:
        return LOCAL_IMPLEMENTATION
    return CHATGPT_IMPLEMENTATION
