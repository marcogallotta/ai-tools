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
class Event:
    event_gid: str
    event_type: EventType
    identity: Identity
    occurred_at: str
    actor: str
    successor_generation_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.event_gid, "event_gid")
        _text(self.occurred_at, "occurred_at")
        _text(self.actor, "actor")
        if self.successor_generation_id is not None and self.event_type is not EventType.SUPERSEDED:
            raise ValueError("successor_generation_id is valid only on SUPERSEDED")


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
    key = "canonical_snapshot" if record.canonical_snapshot is not None else "canonical_snapshot_ref"
    result[key] = record.canonical_snapshot or record.canonical_snapshot_ref
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
    return result


def recover_snapshot(record: Generation, loader: Callable[[str], bytes] | None = None) -> bytes:
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
        return Recovery(GENERATION_SCHEMA, recover_snapshot(generation, loader), generation.identity)
    if generic_preimage is None:
        raise ValueError("non-design recovery requires generic notes preimage")
    return Recovery("generic-notes-preimage", generic_preimage, None)


def reconstruct(record: Generation, events: Sequence[Event]) -> Reconstruction:
    state: State | None = None
    valid: list[str] = []
    contradictions: list[Contradiction] = []
    seen: set[str] = set()
    latest: str | None = None
    for index, event in enumerate(events):
        if event.event_gid in seen:
            contradictions.append(_c("duplicate-event", event.event_gid, "event GID repeated"))
            continue
        seen.add(event.event_gid)
        if event.identity != record.identity:
            contradictions.append(_c("identity-mismatch", event.event_gid, "event identity disagrees with generation"))
            continue
        next_state, error = _transition(state, event.event_type, index)
        if error:
            contradictions.append(_c("invalid-transition", event.event_gid, error))
            continue
        state = next_state
        latest = event.event_gid
        valid.append(event.event_gid)
    if not events:
        contradictions.append(_c("missing-created-event", record.generation_id, "generation has no events"))
    elif state is None:
        contradictions.append(_c("no-valid-state", record.generation_id, "no valid state reconstructed"))
    return Reconstruction(record.identity, state, latest, tuple(valid), tuple(contradictions))


def _transition(state: State | None, event: EventType, index: int) -> tuple[State | None, str | None]:
    if state is None:
        return (State.AUTHORING, None) if index == 0 and event is EventType.CREATED else (None, "first valid event must be CREATED")
    if state in {State.SUPERSEDED, State.CANCELLED}:
        return state, f"{state.value} is terminal"
    if event is EventType.CREATED:
        return state, "CREATED may occur only once"
    if event is EventType.MARCO_APPROVED:
        return (State.MARCO_APPROVED, None) if state is State.AUTHORING else (state, "MARCO_APPROVED requires AUTHORING")
    if event is EventType.DISPATCHED:
        return (State.DISPATCHED, None) if state is State.MARCO_APPROVED else (state, "DISPATCHED requires MARCO_APPROVED")
    if event is EventType.REOPENED:
        allowed = state in {State.MARCO_APPROVED, State.DISPATCHED}
        return (State.AUTHORING, None) if allowed else (state, "REOPENED requires approved or dispatched state")
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
    for record in records:
        if record.generation_id in by_id:
            problems.append(_c("generation-redefinition", record.generation_id, "generation_id is not unique"))
            continue
        by_id[record.generation_id] = record
        if record.predecessor_generation_id:
            children.setdefault(record.predecessor_generation_id, []).append(record.generation_id)
    for record in by_id.values():
        predecessor_id = record.predecessor_generation_id
        if not predecessor_id:
            continue
        predecessor = by_id.get(predecessor_id)
        if predecessor is None:
            problems.append(_c("missing-predecessor", record.generation_id, predecessor_id))
        elif predecessor.task_gid != record.task_gid:
            problems.append(_c("cross-task-predecessor", record.generation_id, predecessor_id))
    for parent, successors in children.items():
        if len(successors) > 1:
            problems.append(_c("lineage-fork", parent, ",".join(sorted(successors))))
    for generation_id in by_id:
        seen: set[str] = set()
        cursor: str | None = generation_id
        while cursor in by_id:
            if cursor in seen:
                problems.append(_c("lineage-cycle", generation_id, cursor))
                break
            seen.add(cursor)
            cursor = by_id[cursor].predecessor_generation_id
    if states:
        for record in by_id.values():
            predecessor_id = record.predecessor_generation_id
            if predecessor_id and states.get(predecessor_id) and states[predecessor_id].state is State.DISPATCHED:
                problems.append(_c("dispatched-successor-without-reopen", record.generation_id, predecessor_id))
    return tuple(problems)


def projection_contradictions(
    record: Generation,
    state: Reconstruction,
    projection: Projection,
    authoritative_record_gid: str,
) -> tuple[Contradiction, ...]:
    problems: list[Contradiction] = []
    if projection.identity != record.identity:
        problems.append(_c("projection-identity-mismatch", "pointer", "identity"))
    if projection.generation_record_gid != authoritative_record_gid:
        problems.append(_c("projection-record-mismatch", "pointer", "record"))
    if projection.reconstructed_state is not state.state:
        problems.append(_c("projection-state-mismatch", "pointer", "state"))
    if projection.latest_event_gid != state.latest_event_gid:
        problems.append(_c("projection-event-mismatch", "pointer", "latest event"))
    return tuple(problems)


def external_snapshot_contradictions(record: Generation, snapshot: bytes, source: str) -> tuple[Contradiction, ...]:
    _text(source, "source")
    if digest(snapshot) == record.canonical_sha256:
        return ()
    return (_c("competing-design-snapshot", source, f"Review V2 generation {record.generation_id} remains authoritative"),)


def consume_identity(record: Generation) -> Identity:
    return record.identity


def cumulative_drift_baseline(
    current_generation_id: str,
    records: Sequence[Generation],
    histories: Mapping[str, Sequence[Event]],
) -> Generation | None:
    by_id = {record.generation_id: record for record in records}
    cursor = by_id.get(current_generation_id)
    seen: set[str] = set()
    while cursor and cursor.generation_id not in seen:
        seen.add(cursor.generation_id)
        events = histories.get(cursor.generation_id, ())
        rebuilt = reconstruct(cursor, events)
        valid = set(rebuilt.valid_event_gids)
        if any(e.event_gid in valid and e.event_type is EventType.MARCO_APPROVED for e in events):
            return cursor
        cursor = by_id.get(cursor.predecessor_generation_id or "")
    return None


def challenge_used(key: ChallengeKey, history: Iterable[Challenge]) -> bool:
    return any(item.key == key for item in history)


def require_challenge_available(key: ChallengeKey, history: Iterable[Challenge]) -> None:
    if challenge_used(key, history):
        raise ValueError("challenge budget already used for exact candidate/blocker/evidence set")


def validate_disposition(
    challenge: Challenge,
    disposition: ReviewDisposition,
    cumulative_authors: Iterable[str],
) -> None:
    if disposition.key != challenge.key:
        raise ValueError("disposition must bind exact challenge key")
    if disposition.reviewer in {author.strip() for author in cumulative_authors if author.strip()}:
        raise ValueError("material candidate author cannot independently clear candidate")


def _c(code: str, source: str, message: str) -> Contradiction:
    return Contradiction(code, source, message)
