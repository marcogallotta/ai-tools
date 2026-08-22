"""Review V2 exact design-generation lineage mechanics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

GENERATION_SCHEMA = "dish-design-generation:v1"
EVENT_SCHEMA = "dish-design-generation-event:v1"
POINTER_SCHEMA = "dish-design-generation-pointer:v1"
HUMAN_DECISION_SCHEMA = "dish-human-decision-provenance:v1"
SOURCE_POLICY_SCHEMA = "dish-source-policy-registry:v1"
DESIGN_PROVENANCE_SCHEMA = "dish-design-provenance:v1"
_SHA = re.compile(r"^[0-9a-f]{64}$")


class EventType(str, Enum):
    CREATED = "CREATED"
    MARCO_APPROVED = "MARCO_APPROVED"
    DISPATCHED = "DISPATCHED"
    REOPENED = "REOPENED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class State(str, Enum):
    AUTHORING = "AUTHORING"
    MARCO_APPROVED = "MARCO_APPROVED"
    DISPATCHED = "DISPATCHED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class Disposition(str, Enum):
    UPHOLDS = "UPHOLDS"
    NARROWS = "NARROWS"
    REFRAMES = "REFRAMES"
    WITHDRAWS = "WITHDRAWS"


class SourceDisposition(str, Enum):
    ALLOWED = "ALLOWED"
    CAUTION = "CAUTION"
    DISALLOWED_AS_PRECEDENT = "DISALLOWED_AS_PRECEDENT"


class EnvironmentApplicability(str, Enum):
    VERIFIED_AVAILABLE = "VERIFIED_AVAILABLE"
    VERIFIED_UNAVAILABLE = "VERIFIED_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class SourceUse(str, Enum):
    FACTUAL = "FACTUAL"
    NORMATIVE = "NORMATIVE"


class SourceClass(str, Enum):
    MARCO_DECISION = "MARCO_DECISION"
    DISH_INCIDENT_EVIDENCE = "DISH_INCIDENT_EVIDENCE"
    EXTERNAL_PRIMARY_EVIDENCE = "EXTERNAL_PRIMARY_EVIDENCE"
    DISH_LOCAL_INFERENCE = "DISH_LOCAL_INFERENCE"


@dataclass(frozen=True, slots=True)
class Identity:
    task_gid: str
    generation_id: str
    canonical_sha256: str
    relevant_repo_baseline: str | None

    def __post_init__(self) -> None:
        _text(self.task_gid, "task_gid")
        _text(self.generation_id, "generation_id")
        _sha(self.canonical_sha256)
        if self.relevant_repo_baseline is not None:
            _text(self.relevant_repo_baseline, "relevant_repo_baseline")

    def tuple(self) -> tuple[str, str, str, str | None]:
        return (
            self.task_gid,
            self.generation_id,
            self.canonical_sha256,
            self.relevant_repo_baseline,
        )


@dataclass(frozen=True, slots=True)
class Generation:
    task_gid: str
    generation_id: str
    predecessor_generation_id: str | None
    canonical_sha256: str
    relevant_repo_baseline: str | None
    created_at: str
    created_by: str
    canonical_snapshot: str | None = None
    canonical_snapshot_ref: str | None = None

    def __post_init__(self) -> None:
        _text(self.task_gid, "task_gid")
        _text(self.generation_id, "generation_id")
        _sha(self.canonical_sha256)
        _text(self.created_at, "created_at")
        _text(self.created_by, "created_by")
        if self.predecessor_generation_id == self.generation_id:
            raise ValueError("generation cannot be its own predecessor")
        if (self.canonical_snapshot is None) == (self.canonical_snapshot_ref is None):
            raise ValueError("exactly one snapshot or durable snapshot reference is required")
        if self.canonical_snapshot is not None:
            if digest(self.canonical_snapshot.encode()) != self.canonical_sha256:
                raise ValueError("canonical snapshot digest mismatch")

    @property
    def identity(self) -> Identity:
        return Identity(
            self.task_gid,
            self.generation_id,
            self.canonical_sha256,
            self.relevant_repo_baseline,
        )


@dataclass(frozen=True, slots=True)
class HumanDecisionProvenance:
    """Independently recovered durable proof of one exact Marco approval."""

    decision_ref: str
    decision_sha256: str
    identity: Identity
    material_delta_set_sha256: str
    decision_kind: str = "MARCO_APPROVAL"
    decided_by: str = "Marco"

    def __post_init__(self) -> None:
        _text(self.decision_ref, "decision_ref")
        _sha(self.decision_sha256)
        _sha(self.material_delta_set_sha256)
        if self.decision_kind != "MARCO_APPROVAL":
            raise ValueError("human decision provenance must describe MARCO_APPROVAL")
        if self.decided_by != "Marco":
            raise ValueError("human decision provenance must be an explicit Marco decision")


@dataclass(frozen=True, slots=True)
class Event:
    event_gid: str
    event_type: EventType
    identity: Identity
    occurred_at: str
    actor: str
    successor_generation_id: str | None = None
    material_delta_set_sha256: str | None = None
    human_decision_ref: str | None = None
    human_decision_sha256: str | None = None

    def __post_init__(self) -> None:
        _text(self.event_gid, "event_gid")
        _text(self.occurred_at, "occurred_at")
        _text(self.actor, "actor")
        if self.event_type is EventType.SUPERSEDED:
            if self.successor_generation_id is None:
                raise ValueError("SUPERSEDED requires successor_generation_id")
            _text(self.successor_generation_id, "successor_generation_id")
            if self.successor_generation_id == self.identity.generation_id:
                raise ValueError("SUPERSEDED successor must differ from current generation")
        elif self.successor_generation_id is not None:
            raise ValueError("successor_generation_id is valid only on SUPERSEDED")

        has_ref = self.human_decision_ref is not None
        has_sha = self.human_decision_sha256 is not None
        if has_ref != has_sha:
            raise ValueError("human decision reference and digest must be supplied together")
        if self.human_decision_ref is not None:
            _text(self.human_decision_ref, "human_decision_ref")
            assert self.human_decision_sha256 is not None
            _sha(self.human_decision_sha256)

        if self.event_type is EventType.MARCO_APPROVED:
            if self.material_delta_set_sha256 is None:
                raise ValueError("MARCO_APPROVED requires material_delta_set_sha256")
            _sha(self.material_delta_set_sha256)
        else:
            if self.material_delta_set_sha256 is not None:
                raise ValueError("material_delta_set_sha256 is valid only on MARCO_APPROVED")
            if self.human_decision_ref is not None:
                raise ValueError("human decision provenance is valid only on MARCO_APPROVED")


@dataclass(frozen=True, slots=True)
class Contradiction:
    code: str
    source: str
    message: str


@dataclass(frozen=True, slots=True)
class Reconstruction:
    identity: Identity
    state: State | None
    latest_event_gid: str | None
    valid_event_gids: tuple[str, ...]
    contradictions: tuple[Contradiction, ...]
    successor_generation_id: str | None = None

    @property
    def valid(self) -> bool:
        return self.state is not None and not self.contradictions


@dataclass(frozen=True, slots=True)
class Projection:
    identity: Identity
    generation_record_gid: str
    reconstructed_state: State
    latest_event_gid: str


@dataclass(frozen=True, slots=True)
class Recovery:
    source: str
    snapshot: bytes
    identity: Identity | None


@dataclass(frozen=True, slots=True)
class ChallengeKey:
    candidate_identity: Identity
    blocker_id: str
    evidence_set_sha256: str

    def __post_init__(self) -> None:
        _text(self.blocker_id, "blocker_id")
        _sha(self.evidence_set_sha256)


@dataclass(frozen=True, slots=True)
class Challenge:
    key: ChallengeKey
    challenger: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.challenger, "challenger")
        _sha(self.evidence_sha256)


@dataclass(frozen=True, slots=True)
class ReviewDisposition:
    key: ChallengeKey
    reviewer: str
    disposition: Disposition

    def __post_init__(self) -> None:
        _text(self.reviewer, "reviewer")


@dataclass(frozen=True, slots=True)
class SourcePolicyState:
    source_id: str
    decision_class: str
    disposition: SourceDisposition
    event_id: str


@dataclass(frozen=True, slots=True)
class AffectedClaim:
    task_gid: str
    generation_id: str
    claim_id: str
    support_id: str
    has_independent_support: bool


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")


def _sha(value: str) -> None:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def create_generation(
    *,
    task_gid: str,
    generation_id: str,
    snapshot: bytes,
    predecessor_generation_id: str | None,
    relevant_repo_baseline: str | None,
    created_at: str,
    created_by: str,
) -> Generation:
    try:
        text = snapshot.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("inline canonical snapshots must be UTF-8") from exc
    return Generation(
        task_gid=task_gid,
        generation_id=generation_id,
        predecessor_generation_id=predecessor_generation_id,
        canonical_snapshot=text,
        canonical_snapshot_ref=None,
        canonical_sha256=digest(snapshot),
        relevant_repo_baseline=relevant_repo_baseline,
        created_at=created_at,
        created_by=created_by,
    )


def generation_mapping(record: Generation) -> dict[str, str | None]:
    result: dict[str, str | None] = {
        "schema": GENERATION_SCHEMA,
        "task_gid": record.task_gid,
        "generation_id": record.generation_id,
        "predecessor_generation_id": record.predecessor_generation_id,
        "canonical_sha256": record.canonical_sha256,
        "relevant_repo_baseline": record.relevant_repo_baseline,
        "created_at": record.created_at,
        "created_by": record.created_by,
    }
    if record.canonical_snapshot is not None:
        result["canonical_snapshot"] = record.canonical_snapshot
    else:
        result["canonical_snapshot_ref"] = record.canonical_snapshot_ref
    return result


def event_mapping(event: Event) -> dict[str, str | None]:
    result: dict[str, str | None] = {
        "schema": EVENT_SCHEMA,
        "event_gid": event.event_gid,
        "event_type": event.event_type.value,
        "task_gid": event.identity.task_gid,
        "generation_id": event.identity.generation_id,
        "canonical_sha256": event.identity.canonical_sha256,
        "relevant_repo_baseline": event.identity.relevant_repo_baseline,
        "occurred_at": event.occurred_at,
        "actor": event.actor,
    }
    if event.successor_generation_id is not None:
        result["successor_generation_id"] = event.successor_generation_id
    if event.material_delta_set_sha256 is not None:
        result["material_delta_set_sha256"] = event.material_delta_set_sha256
    if event.human_decision_ref is not None:
        result["human_decision_ref"] = event.human_decision_ref
        result["human_decision_sha256"] = event.human_decision_sha256
    return result


def human_decision_mapping(
    decision: HumanDecisionProvenance,
) -> dict[str, str | None]:
    return {
        "schema": HUMAN_DECISION_SCHEMA,
        "decision_ref": decision.decision_ref,
        "decision_sha256": decision.decision_sha256,
        "decision_kind": decision.decision_kind,
        "decided_by": decision.decided_by,
        "task_gid": decision.identity.task_gid,
        "generation_id": decision.identity.generation_id,
        "canonical_sha256": decision.identity.canonical_sha256,
        "relevant_repo_baseline": decision.identity.relevant_repo_baseline,
        "material_delta_set_sha256": decision.material_delta_set_sha256,
    }


def _record_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def identity_from_mapping(record: Mapping[str, Any]) -> Identity:
    """Parse the canonical four-part Review V2 identity without dropping baseline."""
    baseline = record.get("relevant_repo_baseline")
    if baseline is not None:
        baseline = _record_text(baseline, "relevant_repo_baseline")
    return Identity(
        _record_text(record.get("task_gid"), "task_gid"),
        _record_text(record.get("generation_id"), "generation_id"),
        _record_text(record.get("canonical_sha256"), "canonical_sha256"),
        baseline,
    )


def generation_from_mapping(record: Mapping[str, Any]) -> Generation:
    if record.get("schema") != GENERATION_SCHEMA:
        raise ValueError("mapping is not a Review V2 generation")
    identity = identity_from_mapping(record)
    snapshot = record.get("canonical_snapshot")
    snapshot_ref = record.get("canonical_snapshot_ref")
    if snapshot is not None:
        snapshot = _record_text(snapshot, "canonical_snapshot")
    if snapshot_ref is not None:
        snapshot_ref = _record_text(snapshot_ref, "canonical_snapshot_ref")
    predecessor = record.get("predecessor_generation_id")
    if predecessor is not None:
        predecessor = _record_text(predecessor, "predecessor_generation_id")
    return Generation(
        task_gid=identity.task_gid,
        generation_id=identity.generation_id,
        predecessor_generation_id=predecessor,
        canonical_sha256=identity.canonical_sha256,
        relevant_repo_baseline=identity.relevant_repo_baseline,
        created_at=_record_text(record.get("created_at"), "created_at"),
        created_by=_record_text(record.get("created_by"), "created_by"),
        canonical_snapshot=snapshot,
        canonical_snapshot_ref=snapshot_ref,
    )


def event_from_mapping(record: Mapping[str, Any]) -> Event:
    if record.get("schema") != EVENT_SCHEMA:
        raise ValueError("mapping is not a Review V2 event")
    try:
        event_type = EventType(record.get("event_type"))
    except ValueError as exc:
        raise ValueError("unsupported Review V2 event type") from exc
    return Event(
        event_gid=_record_text(record.get("event_gid"), "event_gid"),
        event_type=event_type,
        identity=identity_from_mapping(record),
        occurred_at=_record_text(record.get("occurred_at"), "occurred_at"),
        actor=_record_text(record.get("actor"), "actor"),
        successor_generation_id=record.get("successor_generation_id"),
        material_delta_set_sha256=record.get("material_delta_set_sha256"),
        human_decision_ref=record.get("human_decision_ref"),
        human_decision_sha256=record.get("human_decision_sha256"),
    )


def human_decision_from_mapping(record: Mapping[str, Any]) -> HumanDecisionProvenance:
    if record.get("schema") != HUMAN_DECISION_SCHEMA:
        raise ValueError("mapping is not human-decision provenance")
    return HumanDecisionProvenance(
        decision_ref=_record_text(record.get("decision_ref"), "decision_ref"),
        decision_sha256=_record_text(record.get("decision_sha256"), "decision_sha256"),
        identity=identity_from_mapping(record),
        material_delta_set_sha256=_record_text(
            record.get("material_delta_set_sha256"),
            "material_delta_set_sha256",
        ),
        decision_kind=_record_text(record.get("decision_kind"), "decision_kind"),
        decided_by=_record_text(record.get("decided_by"), "decided_by"),
    )


def parse_record_envelope(
    payload: bytes,
) -> tuple[Generation | Event | HumanDecisionProvenance, ...]:
    """Recover canonical records embedded in durable prose envelopes."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Review V2 record envelope must be UTF-8") from exc
    decoder = json.JSONDecoder()
    records: list[Generation | Event | HumanDecisionProvenance] = []
    cursor = 0
    while True:
        start = text.find("{", cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        cursor = start + end
        if not isinstance(value, Mapping):
            continue
        schema = value.get("schema")
        if schema == GENERATION_SCHEMA:
            records.append(generation_from_mapping(value))
        elif schema == EVENT_SCHEMA:
            records.append(event_from_mapping(value))
        elif schema == HUMAN_DECISION_SCHEMA:
            records.append(human_decision_from_mapping(value))
    return tuple(records)


def recover_snapshot(
    record: Generation,
    loader: Callable[[str], bytes] | None = None,
) -> bytes:
    if record.canonical_snapshot is not None:
        payload = record.canonical_snapshot.encode()
    else:
        if loader is None:
            raise ValueError("durable snapshot reference requires a loader")
        assert record.canonical_snapshot_ref is not None
        payload = loader(record.canonical_snapshot_ref)
    if digest(payload) != record.canonical_sha256:
        raise ValueError("recovered bytes disagree with Review V2 digest")
    return payload


def recover_notes(
    *,
    design_bearing: bool,
    generation: Generation | None = None,
    generic_preimage: bytes | None = None,
    loader: Callable[[str], bytes] | None = None,
) -> Recovery:
    if design_bearing:
        if generation is None:
            raise ValueError("design-bearing recovery requires Review V2 generation")
        return Recovery(
            GENERATION_SCHEMA,
            recover_snapshot(generation, loader),
            generation.identity,
        )
    if generic_preimage is None:
        raise ValueError("non-design recovery requires generic notes preimage")
    return Recovery("generic-notes-preimage", generic_preimage, None)


def reconstruct(
    record: Generation,
    events: Sequence[Event],
    human_decisions: Mapping[str, HumanDecisionProvenance] | None = None,
) -> Reconstruction:
    state: State | None = None
    valid: list[str] = []
    contradictions: list[Contradiction] = []
    seen: set[str] = set()
    latest: str | None = None
    successor_generation_id: str | None = None
    for event in events:
        if event.event_gid in seen:
            contradictions.append(
                _c("duplicate-event", event.event_gid, "event GID repeated")
            )
            continue
        seen.add(event.event_gid)
        if event.identity != record.identity:
            contradictions.append(
                _c(
                    "identity-mismatch",
                    event.event_gid,
                    "event identity disagrees with generation",
                )
            )
            continue
        if event.event_type is EventType.MARCO_APPROVED:
            provenance_error = _approval_provenance_error(
                event,
                human_decisions or {},
            )
            if provenance_error is not None:
                contradictions.append(
                    _c(
                        "invalid-marco-approval-provenance",
                        event.event_gid,
                        provenance_error,
                    )
                )
                continue
        next_state, error = _transition(state, event.event_type)
        if error:
            contradictions.append(
                _c("invalid-transition", event.event_gid, error)
            )
            continue
        state = next_state
        latest = event.event_gid
        valid.append(event.event_gid)
        if event.event_type is EventType.SUPERSEDED:
            successor_generation_id = event.successor_generation_id
    if not events:
        contradictions.append(
            _c(
                "missing-created-event",
                record.generation_id,
                "generation has no events",
            )
        )
    elif state is None:
        contradictions.append(
            _c(
                "no-valid-state",
                record.generation_id,
                "no valid state reconstructed",
            )
        )
    return Reconstruction(
        record.identity,
        state,
        latest,
        tuple(valid),
        tuple(contradictions),
        successor_generation_id,
    )


def _approval_provenance_error(
    event: Event,
    human_decisions: Mapping[str, HumanDecisionProvenance],
) -> str | None:
    if event.human_decision_ref is None or event.human_decision_sha256 is None:
        return "actor/account attribution is not durable Marco approval provenance"
    decision = human_decisions.get(event.human_decision_ref)
    if decision is None:
        return "referenced Marco approval was not independently recovered"
    if decision.decision_sha256 != event.human_decision_sha256:
        return "event decision digest disagrees with independently recovered decision"
    if decision.identity != event.identity:
        return "human decision identity disagrees with approved generation"
    if decision.material_delta_set_sha256 != event.material_delta_set_sha256:
        return "human decision material-delta set disagrees with approval event"
    return None


def _transition(
    state: State | None,
    event: EventType,
) -> tuple[State | None, str | None]:
    if state is None:
        return (
            (State.AUTHORING, None)
            if event is EventType.CREATED
            else (None, "first valid event must be CREATED")
        )
    if state in {State.SUPERSEDED, State.CANCELLED}:
        return state, f"{state.value} is terminal"
    if event is EventType.CREATED:
        return state, "CREATED may occur only once"
    if event is EventType.MARCO_APPROVED:
        return (
            (State.MARCO_APPROVED, None)
            if state is State.AUTHORING
            else (state, "MARCO_APPROVED requires AUTHORING")
        )
    if event is EventType.DISPATCHED:
        return (
            (State.DISPATCHED, None)
            if state is State.MARCO_APPROVED
            else (state, "DISPATCHED requires MARCO_APPROVED")
        )
    if event is EventType.REOPENED:
        allowed = state in {State.MARCO_APPROVED, State.DISPATCHED}
        return (
            (State.AUTHORING, None)
            if allowed
            else (state, "REOPENED requires approved or dispatched state")
        )
    if event is EventType.SUPERSEDED:
        return State.SUPERSEDED, None
    if event is EventType.CANCELLED:
        return State.CANCELLED, None
    raise AssertionError(event)


def validate_lineage(
    records: Sequence[Generation],
    states: Mapping[str, Reconstruction] | None = None,
) -> tuple[Contradiction, ...]:
    problems: list[Contradiction] = []
    by_id: dict[str, Generation] = {}
    children: dict[str, list[str]] = {}
    task_gids = {record.task_gid for record in records}
    if len(task_gids) > 1:
        problems.append(
            _c(
                "mixed-task-lineage",
                "lineage",
                "validate one task lineage at a time",
            )
        )
        return tuple(problems)
    roots = [
        record.generation_id
        for record in records
        if not record.predecessor_generation_id
    ]
    if len(roots) > 1:
        problems.append(
            _c(
                "multiple-lineage-roots",
                "lineage",
                ",".join(sorted(roots)),
            )
        )
    for record in records:
        if record.generation_id in by_id:
            problems.append(
                _c(
                    "generation-redefinition",
                    record.generation_id,
                    "generation_id is not unique",
                )
            )
            continue
        by_id[record.generation_id] = record
        if record.predecessor_generation_id:
            children.setdefault(
                record.predecessor_generation_id,
                [],
            ).append(record.generation_id)
    for record in by_id.values():
        predecessor_id = record.predecessor_generation_id
        if not predecessor_id:
            continue
        predecessor = by_id.get(predecessor_id)
        if predecessor is None:
            problems.append(
                _c(
                    "missing-predecessor",
                    record.generation_id,
                    predecessor_id,
                )
            )
        elif predecessor.task_gid != record.task_gid:
            problems.append(
                _c(
                    "cross-task-predecessor",
                    record.generation_id,
                    predecessor_id,
                )
            )
    for parent, successors in children.items():
        if len(successors) > 1:
            problems.append(
                _c(
                    "lineage-fork",
                    parent,
                    ",".join(sorted(successors)),
                )
            )
    for generation_id in by_id:
        seen: set[str] = set()
        cursor: str | None = generation_id
        while cursor in by_id:
            if cursor in seen:
                problems.append(
                    _c("lineage-cycle", generation_id, cursor)
                )
                break
            seen.add(cursor)
            cursor = by_id[cursor].predecessor_generation_id

    if states:
        for record in by_id.values():
            reconstruction = states.get(record.generation_id)
            if reconstruction and reconstruction.state is State.SUPERSEDED:
                expected = reconstruction.successor_generation_id
                actual = children.get(record.generation_id, [])
                if expected is None:
                    problems.append(
                        _c(
                            "superseded-successor-missing",
                            record.generation_id,
                            "valid SUPERSEDED state has no successor identity",
                        )
                    )
                elif expected not in actual:
                    actual_text = ",".join(sorted(actual)) if actual else "none"
                    problems.append(
                        _c(
                            "superseded-successor-mismatch",
                            record.generation_id,
                            (
                                f"event names {expected}; "
                                f"lineage children are {actual_text}"
                            ),
                        )
                    )

            predecessor_id = record.predecessor_generation_id
            predecessor_state = (
                states.get(predecessor_id)
                if predecessor_id
                else None
            )
            if (
                predecessor_id
                and predecessor_state
                and predecessor_state.state is State.DISPATCHED
            ):
                problems.append(
                    _c(
                        "dispatched-successor-without-reopen",
                        record.generation_id,
                        predecessor_id,
                    )
                )
    return tuple(problems)


def projection_contradictions(
    record: Generation,
    state: Reconstruction,
    projection: Projection,
    authoritative_record_gid: str,
) -> tuple[Contradiction, ...]:
    problems: list[Contradiction] = []
    if projection.identity != record.identity:
        problems.append(
            _c("projection-identity-mismatch", "pointer", "identity")
        )
    if projection.generation_record_gid != authoritative_record_gid:
        problems.append(
            _c("projection-record-mismatch", "pointer", "record")
        )
    if projection.reconstructed_state is not state.state:
        problems.append(
            _c("projection-state-mismatch", "pointer", "state")
        )
    if projection.latest_event_gid != state.latest_event_gid:
        problems.append(
            _c("projection-event-mismatch", "pointer", "latest event")
        )
    return tuple(problems)


def external_snapshot_contradictions(
    record: Generation,
    snapshot: bytes,
    source: str,
) -> tuple[Contradiction, ...]:
    _text(source, "source")
    if digest(snapshot) == record.canonical_sha256:
        return ()
    return (
        _c(
            "competing-design-snapshot",
            source,
            (
                f"Review V2 generation {record.generation_id} "
                "remains authoritative"
            ),
        ),
    )


def consume_identity(record: Generation) -> Identity:
    return record.identity


def cumulative_drift_baseline(
    current_generation_id: str,
    records: Sequence[Generation],
    histories: Mapping[str, Sequence[Event]],
    human_decisions: Mapping[str, HumanDecisionProvenance] | None = None,
) -> Generation | None:
    by_id = {record.generation_id: record for record in records}
    cursor = by_id.get(current_generation_id)
    seen: set[str] = set()
    while cursor and cursor.generation_id not in seen:
        seen.add(cursor.generation_id)
        events = histories.get(cursor.generation_id, ())
        rebuilt = reconstruct(
            cursor,
            events,
            human_decisions=human_decisions,
        )
        valid = set(rebuilt.valid_event_gids)
        if any(
            event.event_gid in valid
            and event.event_type is EventType.MARCO_APPROVED
            for event in events
        ):
            return cursor
        cursor = by_id.get(cursor.predecessor_generation_id or "")
    return None


def challenge_used(
    key: ChallengeKey,
    history: Iterable[Challenge],
) -> bool:
    return any(item.key == key for item in history)


def require_challenge_available(
    key: ChallengeKey,
    history: Iterable[Challenge],
) -> None:
    if challenge_used(key, history):
        raise ValueError(
            "challenge budget already used for exact candidate/blocker/evidence set"
        )


def validate_disposition(
    challenge: Challenge,
    disposition: ReviewDisposition,
    cumulative_authors: Iterable[str],
) -> None:
    if disposition.key != challenge.key:
        raise ValueError("disposition must bind exact challenge key")
    authors = {
        author.strip()
        for author in cumulative_authors
        if author.strip()
    }
    if disposition.reviewer in authors:
        raise ValueError(
            "material candidate author cannot independently clear candidate"
        )
    if disposition.reviewer == challenge.challenger:
        raise ValueError(
            "challenger cannot supply the independent reviewer disposition"
        )


def validate_source_policy_registry(
    registry: Mapping[str, object],
) -> tuple[Contradiction, ...]:
    """Validate the small repository-owned source-policy registry.

    The registry governs normative precedent eligibility only. It is not design
    authority, a credibility ranking, or a source of environment truth.
    """

    problems: list[Contradiction] = []
    if registry.get("schema") != SOURCE_POLICY_SCHEMA:
        problems.append(_c("source-policy-schema", "registry", "unsupported schema"))
    if registry.get("schema_version") != 1:
        problems.append(
            _c("source-policy-version", "registry", "schema_version must be 1")
        )

    raw_sources = registry.get("sources")
    sources = _mapping_sequence(raw_sources)
    if sources is None:
        problems.append(
            _c("source-policy-sources", "registry", "sources must be an array")
        )
        sources = ()

    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        label = f"source[{index}]"
        source_id = _mapping_text(source, "source_id")
        organization = _mapping_text(source, "organization")
        primary = source.get("primary_source")
        primary_mapping = primary if isinstance(primary, Mapping) else None
        title = _mapping_text(primary_mapping, "title") if primary_mapping else None
        uri = _mapping_text(primary_mapping, "uri") if primary_mapping else None
        version = (
            _mapping_text(primary_mapping, "version_or_date")
            if primary_mapping
            else None
        )
        if not source_id:
            problems.append(_c("source-id-missing", label, "source_id is required"))
        elif source_id in source_ids:
            problems.append(_c("source-id-duplicate", label, source_id))
        else:
            source_ids.add(source_id)
        if not organization:
            problems.append(
                _c("source-organization-missing", label, "organization is required")
            )
        if not all((title, uri, version)):
            problems.append(
                _c(
                    "source-primary-identity-incomplete",
                    label,
                    "primary_source requires title, uri, and version_or_date",
                )
            )

    raw_events = registry.get("disposition_events")
    events = _mapping_sequence(raw_events)
    if events is None:
        problems.append(
            _c(
                "source-policy-events",
                "registry",
                "disposition_events must be an array",
            )
        )
        events = ()

    event_ids: set[str] = set()
    by_scope: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for index, event in enumerate(events):
        label = f"disposition_event[{index}]"
        event_id = _mapping_text(event, "event_id")
        source_id = _mapping_text(event, "source_id")
        decision_class = _mapping_text(event, "decision_class")
        disposition = event.get("disposition")
        predecessor = event.get("predecessor_event_id")
        authority = event.get("authority")

        if not event_id:
            problems.append(_c("source-policy-event-id", label, "event_id is required"))
        elif event_id in event_ids:
            problems.append(_c("source-policy-event-duplicate", label, event_id))
        else:
            event_ids.add(event_id)

        if not source_id or source_id not in source_ids:
            problems.append(
                _c(
                    "source-policy-unknown-source",
                    label,
                    source_id or "missing source_id",
                )
            )
        if not decision_class:
            problems.append(
                _c("source-policy-decision-class", label, "decision_class is required")
            )
        try:
            SourceDisposition(str(disposition))
        except ValueError:
            problems.append(
                _c("source-policy-disposition", label, f"invalid disposition {disposition!r}")
            )
        if predecessor is not None and not _nonempty_text(predecessor):
            problems.append(
                _c(
                    "source-policy-predecessor",
                    label,
                    "predecessor_event_id must be non-empty text or null",
                )
            )
        problems.extend(_source_policy_authority_problems(authority, label))
        if source_id and decision_class:
            by_scope.setdefault((source_id, decision_class), []).append(event)

    for (source_id, decision_class), scoped_events in by_scope.items():
        problems.extend(
            _source_policy_chain_problems(source_id, decision_class, scoped_events)
        )
    return tuple(problems)


def active_source_disposition(
    registry: Mapping[str, object],
    source_id: str,
    decision_class: str,
) -> SourcePolicyState | None:
    """Return the current exact-scope disposition, falling back to global scope.

    Absence is deliberately returned as ``None``; it never means ALLOWED.
    """

    problems = validate_source_policy_registry(registry)
    if problems:
        raise ValueError(
            "invalid source-policy registry: "
            + "; ".join(f"{item.code}:{item.source}" for item in problems)
        )
    events = _mapping_sequence(registry.get("disposition_events")) or ()
    for scope in (decision_class, "*"):
        scoped = [
            event
            for event in events
            if event.get("source_id") == source_id
            and event.get("decision_class") == scope
        ]
        if not scoped:
            continue
        terminal = _terminal_source_policy_event(scoped)
        return SourcePolicyState(
            source_id=source_id,
            decision_class=scope,
            disposition=SourceDisposition(str(terminal["disposition"])),
            event_id=str(terminal["event_id"]),
        )
    return None


def validate_design_provenance(
    record: Mapping[str, object],
    identity: Identity,
    source_policy: Mapping[str, object],
) -> tuple[Contradiction, ...]:
    """Validate claim provenance attached to one exact Review V2 generation."""

    problems: list[Contradiction] = list(validate_source_policy_registry(source_policy))
    if record.get("schema") != DESIGN_PROVENANCE_SCHEMA:
        problems.append(_c("design-provenance-schema", "provenance", "unsupported schema"))
    if _record_identity(record) != identity.tuple():
        problems.append(
            _c(
                "design-provenance-identity-mismatch",
                "provenance",
                "claim provenance must bind the exact Review V2 generation identity",
            )
        )

    claims = _mapping_sequence(record.get("claims"))
    if claims is None:
        return tuple(
            problems
            + [_c("design-provenance-claims", "provenance", "claims must be an array")]
        )

    claim_ids: set[str] = set()
    for claim_index, claim in enumerate(claims):
        claim_id = _mapping_text(claim, "claim_id")
        label = claim_id or f"claim[{claim_index}]"
        if not claim_id:
            problems.append(_c("claim-id-missing", label, "claim_id is required"))
        elif claim_id in claim_ids:
            problems.append(_c("claim-id-duplicate", label, claim_id))
        else:
            claim_ids.add(claim_id)

        for field in (
            "decision",
            "problem_outcome",
            "operator_cost",
            "failure_mode",
            "reversibility",
        ):
            if not _mapping_text(claim, field):
                problems.append(_c(f"claim-{field}-missing", label, f"{field} is required"))
        alternatives = _text_sequence(claim.get("alternatives_considered"))
        assumptions = _text_sequence(claim.get("assumptions"), allow_empty=True)
        if alternatives is None or not alternatives:
            problems.append(
                _c(
                    "claim-alternatives-missing",
                    label,
                    "alternatives_considered must contain at least one entry",
                )
            )
        if assumptions is None:
            problems.append(
                _c("claim-assumptions-invalid", label, "assumptions must be an array of text")
            )

        supports = _mapping_sequence(claim.get("supports"))
        if supports is None or not supports:
            problems.append(
                _c("claim-supports-missing", label, "supports must contain at least one entry")
            )
            supports = ()
        support_ids: set[str] = set()
        for support_index, support in enumerate(supports):
            support_id = _mapping_text(support, "support_id")
            support_label = f"{label}:{support_id or support_index}"
            if not support_id:
                problems.append(
                    _c("support-id-missing", support_label, "support_id is required")
                )
            elif support_id in support_ids:
                problems.append(_c("support-id-duplicate", support_label, support_id))
            else:
                support_ids.add(support_id)
            problems.extend(
                _support_provenance_problems(support, support_label, source_policy)
            )

        mechanisms = _mapping_sequence(claim.get("mechanisms"))
        if mechanisms is None:
            problems.append(
                _c("claim-mechanisms-invalid", label, "mechanisms must be an array")
            )
            mechanisms = ()
        mechanism_ids: set[str] = set()
        for mechanism_index, mechanism in enumerate(mechanisms):
            mechanism_id = _mapping_text(mechanism, "mechanism_id")
            mechanism_label = f"{label}:{mechanism_id or mechanism_index}"
            if not mechanism_id:
                problems.append(
                    _c("mechanism-id-missing", mechanism_label, "mechanism_id is required")
                )
            elif mechanism_id in mechanism_ids:
                problems.append(_c("mechanism-id-duplicate", mechanism_label, mechanism_id))
            else:
                mechanism_ids.add(mechanism_id)
            if not isinstance(mechanism.get("recommended"), bool):
                problems.append(
                    _c(
                        "mechanism-recommended-invalid",
                        mechanism_label,
                        "recommended must be boolean",
                    )
                )
            requirements = _mapping_sequence(mechanism.get("requirements"))
            if requirements is None:
                problems.append(
                    _c(
                        "mechanism-requirements-invalid",
                        mechanism_label,
                        "requirements must be an array",
                    )
                )
                continue
            for requirement_index, requirement in enumerate(requirements):
                problems.extend(
                    _environment_requirement_problems(
                        requirement,
                        f"{mechanism_label}:requirement[{requirement_index}]",
                        recommended=mechanism.get("recommended") is True,
                    )
                )
    return tuple(problems)


def affected_claims_for_source_policy(
    records: Sequence[Mapping[str, object]],
    current_identities: Mapping[str, Identity],
    *,
    source_id: str,
    decision_class: str,
) -> tuple[AffectedClaim, ...]:
    """Bounded reverse lookup over supplied provenance records only.

    Search/discovery is not authority: a record is eligible only when its exact
    Review V2 identity equals the caller-supplied current identity for that task.
    """

    affected: list[AffectedClaim] = []
    for record in records:
        task_gid = _mapping_text(record, "task_gid")
        if not task_gid:
            continue
        current = current_identities.get(task_gid)
        if current is None or _record_identity(record) != current.tuple():
            continue
        claims = _mapping_sequence(record.get("claims")) or ()
        for claim in claims:
            claim_id = _mapping_text(claim, "claim_id") or ""
            supports = _mapping_sequence(claim.get("supports")) or ()
            matching = [
                support
                for support in supports
                if support.get("source_class") == SourceClass.EXTERNAL_PRIMARY_EVIDENCE.value
                and support.get("source_use") == SourceUse.NORMATIVE.value
                and support.get("source_id") == source_id
                and (
                    decision_class == "*"
                    or support.get("decision_class") == decision_class
                )
            ]
            if not matching:
                continue
            for support in matching:
                support_id = _mapping_text(support, "support_id") or ""
                independent = any(
                    candidate is not support
                    and candidate.get("support_id") != support_id
                    and _mapping_text(candidate, "support_id")
                    for candidate in supports
                )
                affected.append(
                    AffectedClaim(
                        task_gid=task_gid,
                        generation_id=current.generation_id,
                        claim_id=claim_id,
                        support_id=support_id,
                        has_independent_support=independent,
                    )
                )
    return tuple(affected)


def _support_provenance_problems(
    support: Mapping[str, object],
    label: str,
    source_policy: Mapping[str, object],
) -> list[Contradiction]:
    problems: list[Contradiction] = []
    source_class_raw = support.get("source_class")
    try:
        source_class = SourceClass(str(source_class_raw))
    except ValueError:
        problems.append(
            _c("support-source-class", label, f"invalid source_class {source_class_raw!r}")
        )
        return problems
    refs = _text_sequence(support.get("evidence_refs"), allow_empty=True)
    if refs is None:
        problems.append(
            _c("support-evidence-refs", label, "evidence_refs must be an array of text")
        )

    if source_class is not SourceClass.EXTERNAL_PRIMARY_EVIDENCE:
        if support.get("source_id") is not None or support.get("source_use") is not None:
            problems.append(
                _c(
                    "support-external-fields-on-local-source",
                    label,
                    "source_id/source_use are only valid for EXTERNAL_PRIMARY_EVIDENCE",
                )
            )
        return problems

    source_id = _mapping_text(support, "source_id")
    decision_class = _mapping_text(support, "decision_class")
    source_statement = _mapping_text(support, "source_statement")
    dish_inference = _mapping_text(support, "dish_inference")
    use_raw = support.get("source_use")
    try:
        source_use = SourceUse(str(use_raw))
    except ValueError:
        problems.append(_c("support-source-use", label, f"invalid source_use {use_raw!r}"))
        return problems
    source_ids = {
        item.get("source_id")
        for item in (_mapping_sequence(source_policy.get("sources")) or ())
    }
    if not source_id or source_id not in source_ids:
        problems.append(
            _c("support-source-id", label, source_id or "source_id is required")
        )
    if not source_statement:
        problems.append(
            _c(
                "support-source-statement",
                label,
                "source_statement must state what the primary source actually supports",
            )
        )
    if not dish_inference:
        problems.append(
            _c(
                "support-dish-inference",
                label,
                "dish_inference must state the Dish extrapolation, including 'none'",
            )
        )
    if source_use is SourceUse.FACTUAL:
        return problems

    if not decision_class:
        problems.append(
            _c(
                "support-decision-class",
                label,
                "normative external support requires decision_class",
            )
        )
        return problems
    if not source_id:
        return problems
    try:
        current = active_source_disposition(source_policy, source_id, decision_class)
    except ValueError:
        # Registry-level contradictions are already emitted by the enclosing
        # validator. Do not turn malformed policy evidence into an exception
        # that hides those exact defects.
        return problems
    policy = support.get("source_policy")
    policy_mapping = policy if isinstance(policy, Mapping) else None
    observed = (
        _mapping_text(policy_mapping, "observed_disposition")
        if policy_mapping
        else None
    )
    observed_event = policy_mapping.get("event_id") if policy_mapping else None
    expected = current.disposition.value if current else "NO_ACTIVE_DISPOSITION"
    expected_event = current.event_id if current else None
    if observed != expected or observed_event != expected_event:
        problems.append(
            _c(
                "source-policy-stale-or-missing",
                label,
                f"recorded source policy {observed!r}/{observed_event!r} "
                f"does not match current {expected!r}/{expected_event!r}",
            )
        )
    if current and current.disposition is SourceDisposition.DISALLOWED_AS_PRECEDENT:
        problems.append(
            _c(
                "disallowed-normative-source",
                label,
                "source is disallowed as normative precedent for this decision class",
            )
        )
    if current and current.disposition is SourceDisposition.CAUTION:
        if not _mapping_text(support, "caution_acknowledgement"):
            problems.append(
                _c(
                    "source-caution-unacknowledged",
                    label,
                    "CAUTION disposition must be explicitly addressed",
                )
            )
    return problems


def _environment_requirement_problems(
    requirement: Mapping[str, object],
    label: str,
    *,
    recommended: bool,
) -> list[Contradiction]:
    problems: list[Contradiction] = []
    for field in ("capability", "target_surface", "refresh_trigger"):
        if not _mapping_text(requirement, field):
            problems.append(_c(f"environment-{field}", label, f"{field} is required"))
    if not isinstance(requirement.get("required"), bool):
        problems.append(
            _c("environment-required", label, "required must be boolean")
        )
    status_raw = requirement.get("status")
    try:
        status = EnvironmentApplicability(str(status_raw))
    except ValueError:
        problems.append(
            _c("environment-status", label, f"invalid status {status_raw!r}")
        )
        return problems
    if status is not EnvironmentApplicability.UNKNOWN:
        if not _mapping_text(requirement, "evidence_ref") or not _mapping_text(
            requirement, "evidence_as_of"
        ):
            problems.append(
                _c(
                    "environment-evidence",
                    label,
                    "verified environment status requires evidence_ref and evidence_as_of",
                )
            )
    if recommended and requirement.get("required") is True:
        if status is EnvironmentApplicability.UNKNOWN:
            problems.append(
                _c(
                    "required-environment-unknown",
                    label,
                    "required UNKNOWN capability cannot support a recommended mechanism",
                )
            )
        elif status is EnvironmentApplicability.VERIFIED_UNAVAILABLE:
            problems.append(
                _c(
                    "required-environment-unavailable",
                    label,
                    "required VERIFIED_UNAVAILABLE capability rejects the mechanism here",
                )
            )
    return problems


def _source_policy_authority_problems(
    authority: object,
    label: str,
) -> list[Contradiction]:
    if not isinstance(authority, Mapping):
        return [
            _c(
                "source-policy-human-authority",
                label,
                "durable explicit human authority is required",
            )
        ]
    authority_type = _mapping_text(authority, "authority_type")
    durable_ref = _mapping_text(authority, "durable_ref")
    decided_by = _mapping_text(authority, "decided_by")
    decision = _mapping_text(authority, "decision")
    decision_sha = _mapping_text(authority, "decision_sha256")
    effective_at = _mapping_text(authority, "effective_at")
    problems: list[Contradiction] = []
    if authority_type not in {"MARCO_EXPLICIT", "AUTHORIZED_HUMAN_EXPLICIT"}:
        problems.append(
            _c(
                "source-policy-human-authority",
                label,
                "authenticated account attribution is not source-policy authority",
            )
        )
    if not all((durable_ref, decided_by, decision, effective_at)):
        problems.append(
            _c(
                "source-policy-authority-fields",
                label,
                "durable_ref, decided_by, decision, and effective_at are required",
            )
        )
    if decision:
        expected = digest(decision.encode())
        if decision_sha != expected:
            problems.append(
                _c(
                    "source-policy-authority-digest",
                    label,
                    "decision_sha256 must bind exact durable decision text",
                )
            )
    elif decision_sha:
        problems.append(
            _c("source-policy-authority-digest", label, "decision text is missing")
        )
    return problems


def _source_policy_chain_problems(
    source_id: str,
    decision_class: str,
    events: Sequence[Mapping[str, object]],
) -> list[Contradiction]:
    label = f"source-policy:{source_id}:{decision_class}"
    by_id = {
        str(event["event_id"]): event
        for event in events
        if _mapping_text(event, "event_id")
    }
    children: dict[str, list[str]] = {event_id: [] for event_id in by_id}
    roots: list[str] = []
    problems: list[Contradiction] = []
    for event_id, event in by_id.items():
        predecessor = event.get("predecessor_event_id")
        if predecessor is None:
            roots.append(event_id)
        elif predecessor not in by_id:
            problems.append(
                _c(
                    "source-policy-missing-predecessor",
                    label,
                    f"{event_id} references missing predecessor {predecessor}",
                )
            )
        else:
            children[str(predecessor)].append(event_id)
    if len(roots) != 1:
        problems.append(
            _c(
                "source-policy-root-count",
                label,
                f"expected one disposition-history root, found {len(roots)}",
            )
        )
    if any(len(items) > 1 for items in children.values()):
        problems.append(
            _c(
                "source-policy-history-fork",
                label,
                "disposition history must be one explicit supersession chain",
            )
        )
    if roots:
        seen: set[str] = set()
        cursor = roots[0]
        while cursor not in seen:
            seen.add(cursor)
            next_items = children.get(cursor, [])
            if not next_items:
                break
            cursor = next_items[0]
        if cursor in seen and children.get(cursor):
            problems.append(
                _c("source-policy-history-cycle", label, "disposition history has a cycle")
            )
        if len(seen) != len(by_id):
            problems.append(
                _c(
                    "source-policy-history-disconnected",
                    label,
                    "all disposition events must participate in the same chain",
                )
            )
    return problems


def _terminal_source_policy_event(
    events: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    predecessor_ids = {
        str(event["predecessor_event_id"])
        for event in events
        if event.get("predecessor_event_id") is not None
    }
    terminals = [event for event in events if str(event["event_id"]) not in predecessor_ids]
    if len(terminals) != 1:
        raise ValueError("source-policy scope has no unique terminal event")
    return terminals[0]


def _record_identity(record: Mapping[str, object]) -> tuple[object, object, object, object]:
    return (
        record.get("task_gid"),
        record.get("generation_id"),
        record.get("canonical_sha256"),
        record.get("relevant_repo_baseline"),
    )


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    result: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        result.append(item)
    return tuple(result)


def _text_sequence(value: object, *, allow_empty: bool = False) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    result: list[str] = []
    for item in value:
        if not _nonempty_text(item):
            return None
        result.append(str(item))
    if not result and not allow_empty:
        return ()
    return tuple(result)


def _mapping_text(mapping: Mapping[str, object] | None, key: str) -> str | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    return str(value) if _nonempty_text(value) else None


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _c(code: str, source: str, message: str) -> Contradiction:
    return Contradiction(code, source, message)
