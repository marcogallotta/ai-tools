"""Domain models and routing helpers for the guarded dish workflow."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

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


class ReadOnlyLegacyAdapter(Mapping[str, Any]):
    """Explicit read-only view of deprecated validation metadata."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = copy.deepcopy(dict(data))

    def __getitem__(self, key: str) -> Any:
        return copy.deepcopy(self._data[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


@dataclass(frozen=True)
class ResolvedRelease:
    """One current Honest compatibility resolution.

    ``protocols`` contains at most the single stage protocol requested by the
    caller. ``manifests`` is a transitional projection from the authoritative
    Honest task schema for commands not yet converted by later rollout steps.
    """

    version: str
    commit: str
    root: Path
    protocols: Mapping[str, str]
    manifests: Mapping[str, Mapping[str, Any]]
    manifest_texts: Mapping[str, str]
    schema_version: str = ""
    schema: Mapping[str, Any] = field(default_factory=dict)
    schema_text: str = ""
    migration_metadata: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    requested_protocol_role: str | None = None

    @property
    def protocol_version(self) -> str:
        return self.version

    def protocol_for_role(self, role: str) -> str:
        try:
            return self.protocols[role]
        except KeyError as exc:
            raise DishRuleError(
                "VALIDATION_FAILED",
                f"the {role} protocol was not loaded for this command",
                rule="protocol_not_loaded",
                details={"requested_role": role},
            ) from exc

    def bundle_for_submission(self, submission_kind: str) -> dict[str, str]:
        """Return the one stage protocol needed by the legacy start envelope.

        This method deliberately no longer returns a Research+Verification
        bundle. It exists only until the later command-lifecycle rewrite removes
        the legacy database column.
        """

        if submission_kind == "planning":
            role = "planning"
        elif submission_kind in {"initial", "change"}:
            role = "research"
        else:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"unknown submission kind: {submission_kind!r}",
                rule="invalid_submission_kind",
            )
        return {role: self.protocol_for_role(role)}

    def manifest_for_submission(self, submission_kind: str) -> Mapping[str, Any]:
        if submission_kind == "planning":
            key = "planning"
        elif submission_kind in {"initial", "change"}:
            key = "complete_task"
        else:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"unknown submission kind: {submission_kind!r}",
                rule="invalid_submission_kind",
            )
        try:
            return copy.deepcopy(self.manifests[key])
        except KeyError as exc:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "the current task schema has no legacy validation adapter",
                rule="schema_adapter_missing",
                details={"adapter": key},
            ) from exc


@dataclass(frozen=True)
class VerificationProtocolSnapshot:
    identity: str
    text: str
    source: str


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
class ContentIdentity:
    """Stable identity of the exact live task title and notes."""

    digest: str
    title: str
    notes: str


@dataclass(frozen=True)
class OperationActors:
    editor_agent: str | None = None
    researcher_agent: str | None = None
    verifier_agent: str | None = None
    run_id: str | None = None
    independence_attestation: str | None = None




@dataclass(frozen=True)
class VerifierIdentity:
    agent: str
    run_id: str | None = None
    independence_attestation: str | None = None

    def validate(
        self, *, editor_agent: str | None, researcher_agent: str | None,
        constructor_run_id: str | None = None,
    ) -> None:
        agent_family(self.agent)
        run_id = str(self.run_id or "").strip()
        if not run_id:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "a verifier run ID is required",
                rule="verifier_identity_required",
            )
        constructor_run = str(constructor_run_id or "").strip()
        if run_id and constructor_run and run_id == constructor_run:
            raise DishRuleError(
                "AGENT_MISMATCH",
                "the constructor or material editor run cannot verify the candidate",
                rule="verifier_not_independent",
                details={"verifier_run_id": run_id},
            )



FAMILY_DISPLAY_NAMES = {"claude": "Claude", "gpt": "Custom GPT", "codex": "Codex"}

MAX_ACTOR_MODEL_LENGTH = 80


def validate_actor_model(model: str) -> str:
    clean = str(model or "").strip()
    if not clean:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "a model is required to record actor provenance",
            rule="model_required",
        )
    if len(clean) > MAX_ACTOR_MODEL_LENGTH:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"model exceeds the maximum length of {MAX_ACTOR_MODEL_LENGTH} characters",
            rule="model_too_long",
        )
    if "—" in clean or "," in clean:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "model must not contain an em dash or comma",
            rule="model_invalid_characters",
        )
    return clean


def material_editor_line(agent: str, model: str, date: str) -> str:
    agent_family(agent)
    clean_model = validate_actor_model(model)
    return f"{FAMILY_DISPLAY_NAMES[agent]} — {clean_model}, {date}"

def verification_actor_line(agent: str, model: str, date: str) -> str:
    agent_family(agent)
    clean_model = validate_actor_model(model)
    return f"{FAMILY_DISPLAY_NAMES[agent]} — {clean_model}, {date}"


def material_change_line(
    agent: str,
    model: str,
    date: str,
    *,
    change: str,
    reason: str,
    materiality: str,
    verified: bool = False,
) -> str:
    agent_family(agent)
    clean_model = validate_actor_model(model)
    if materiality not in {"Small", "Large"}:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "materiality must be Small or Large",
            rule="material_change_materiality_invalid",
        )
    clean_change = str(change or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_change or not clean_reason:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "material change and reason are required",
            rule="material_change_detail_required",
        )
    verification_state = "pending-verification"
    if verified:
        verification_state = (
            f"verified — {FAMILY_DISPLAY_NAMES[agent]}, {clean_model}, {date}"
        )
    return (
        f"{date} — {FAMILY_DISPLAY_NAMES[agent]} — {clean_model} — "
        f"{clean_change} — {clean_reason} — {materiality} — {verification_state}"
    )


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    task_gid: str
    operation_kind: str
    status: str
    expected_identity: str
    schema_version: str


@dataclass(frozen=True)
class VerificationCycleRecord:
    cycle_id: str
    operation_id: str
    task_gid: str
    cycle_number: int
    protocol_release: str
    correction_class: str | None
    outcome: str | None
    route: str | None
    resume_state: str | None


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

# Canonical task-document models live in a separate module to keep legacy
# operation-state models isolated during the lifecycle rewrite.
from .task_document import (  # noqa: E402,F401
    CanonicalTaskDocument,
    DocumentFinding,
    DocumentValidation,
    FindingKind,
    PlanningBrief,
    TaskState,
)
