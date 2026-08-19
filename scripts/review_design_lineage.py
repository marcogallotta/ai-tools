"""Review V2 exact design-generation lineage mechanics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import re
from typing import Callable, Iterable, Mapping, Sequence

GENERATION_SCHEMA = "dish-design-generation:v1"
EVENT_SCHEMA = "dish-design-generation-event:v1"
POINTER_SCHEMA = "dish-design-generation-pointer:v1"
HUMAN_DECISION_SCHEMA = "dish-human-decision-provenance:v1"
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


def _c(code: str, source: str, message: str) -> Contradiction:
    return Contradiction(code, source, message)
