"""Domain models and routing helpers for the guarded dish workflow."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from .constants import AGENT_FAMILIES
from .errors import DishRuleError


@dataclass(frozen=True)
class Section:
    gid: str
    name: str


@dataclass(frozen=True)
class SectionRegistry:
    by_name: Mapping[str, Section]
    by_gid: Mapping[str, Section]
    research_queue_gid: str
    verification_queue_gid: str
    sourcing_gid: str
    reference_gid: str

    @classmethod
    def from_sections(cls, sections: Iterable[Mapping[str, Any]]) -> "SectionRegistry":
        by_name: dict[str, Section] = {}
        by_gid: dict[str, Section] = {}
        for raw in sections:
            gid = str(raw.get("gid") or "").strip()
            name = str(raw.get("name") or "").strip()
            if not gid or not name:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "section is missing an immutable GID or display name",
                    rule="section_malformed",
                )
            if name in by_name or gid in by_gid:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    f"ambiguous Cooking section: {name!r} / {gid!r}",
                    rule="section_ambiguous",
                )
            section = Section(gid=gid, name=name)
            by_name[name] = section
            by_gid[gid] = section

        required = ("Research Queue", "Verification Queue", "Sourcing", "Reference")
        missing = [name for name in required if name not in by_name]
        if missing:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "required Cooking sections are missing",
                rule="section_missing",
                details={"sections": missing},
            )
        return cls(
            by_name=dict(by_name),
            by_gid=dict(by_gid),
            research_queue_gid=by_name["Research Queue"].gid,
            verification_queue_gid=by_name["Verification Queue"].gid,
            sourcing_gid=by_name["Sourcing"].gid,
            reference_gid=by_name["Reference"].gid,
        )

    @property
    def excluded_gids(self) -> frozenset[str]:
        return frozenset({self.sourcing_gid, self.reference_gid})

    @property
    def queue_gids(self) -> frozenset[str]:
        return frozenset({self.research_queue_gid, self.verification_queue_gid})


@dataclass(frozen=True)
class ResolvedRelease:
    version: str
    commit: str
    root: Path
    protocols: Mapping[str, str]
    manifests: Mapping[str, Mapping[str, Any]]
    manifest_texts: Mapping[str, str]

    def bundle_for_submission(self, submission_kind: str) -> dict[str, str]:
        if submission_kind == "planning":
            return {"planning": self.protocols["planning"]}
        if submission_kind in {"initial", "change"}:
            return {
                "research": self.protocols["research"],
                "verification": self.protocols["verification"],
            }
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"unknown submission kind: {submission_kind!r}",
            rule="invalid_submission_kind",
        )

    def manifest_for_submission(self, submission_kind: str) -> Mapping[str, Any]:
        if submission_kind == "planning":
            return copy.deepcopy(self.manifests["planning"])
        if submission_kind in {"initial", "change"}:
            return copy.deepcopy(self.manifests["complete_task"])
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"unknown submission kind: {submission_kind!r}",
            rule="invalid_submission_kind",
        )


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[dict[str, Any], ...]
    exemption_tags: tuple[str, ...] | None = None
    destination_name: str | None = None
    destination_gid: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class TitleFields:
    role_tags: tuple[str, ...]
    blockers: tuple[str, ...]
    dish_name: str
    recognition: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "role_tags": list(self.role_tags),
            "blockers": list(self.blockers),
            "dish_name": self.dish_name,
            "recognition": self.recognition,
        }


@dataclass(frozen=True)
class TitleValidationResult:
    errors: tuple[dict[str, Any], ...]
    title: str | None = None
    fields: TitleFields | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.title is not None and self.fields is not None


@dataclass(frozen=True)
class ProcessIdentity:
    hostname: str
    pid: int
    process_start: str


@dataclass(frozen=True)
class WriteAttempt:
    attempt_id: str
    started_at: str
    identity: ProcessIdentity


class RequestPhase(str, Enum):
    PRE_SEND = "pre_send"
    POSSIBLY_SENT = "possibly_sent"
    RESPONSE_RECEIVED = "response_received"


@dataclass
class RequestPhaseTracker:
    phase: RequestPhase = RequestPhase.PRE_SEND

    def mark_send_started(self) -> None:
        self.phase = RequestPhase.POSSIBLY_SENT

    def mark_response_received(self) -> None:
        self.phase = RequestPhase.RESPONSE_RECEIVED


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def agent_family(agent: str) -> str:
    try:
        return AGENT_FAMILIES[agent]
    except KeyError as exc:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"invalid agent {agent!r}; expected claude, gpt, or codex",
            rule="invalid_agent",
        ) from exc


def opposite_family(family: str) -> str:
    if family == "claude":
        return "gpt"
    if family == "gpt":
        return "claude"
    raise DishRuleError(
        "INVALID_ARGUMENT",
        f"invalid agent family: {family!r}",
        rule="invalid_agent_family",
    )


def is_protocol_managed(
    current_section_gid: str | None, registry: SectionRegistry
) -> bool:
    """Fail closed: unresolved section membership is managed."""

    if not current_section_gid:
        return True
    return str(current_section_gid) not in registry.excluded_gids


def resolve_destination(name: str, gid: str, registry: SectionRegistry) -> Section:
    clean_name = str(name).strip()
    clean_gid = str(gid).strip()
    section_by_name = registry.by_name.get(clean_name)
    section_by_gid = registry.by_gid.get(clean_gid)
    if section_by_name is None or section_by_gid is None:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "Destination section does not resolve inside Cooking",
            rule="destination_unresolved",
            details={"name": clean_name, "gid": clean_gid},
        )
    if section_by_name.gid != clean_gid or section_by_gid.name != clean_name:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "Destination section name/GID pair does not match",
            rule="destination_mismatch",
            details={"name": clean_name, "gid": clean_gid},
        )
    if clean_gid in registry.queue_gids:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "Destination section must not be a workflow queue",
            rule="destination_is_queue",
            details={"name": clean_name, "gid": clean_gid},
        )
    return section_by_gid
