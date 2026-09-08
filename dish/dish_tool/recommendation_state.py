"""Bounded cooking recommendation state compiled from immutable evidence.

This module is deliberately a projection layer. It does not persist evidence, write
Scratchpad/profile state, or own safety/halal policy. Callers supply immutable evidence
and authoritative eligibility observations; the reducer produces deterministic current
state and a bounded soft-signal payload.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum, IntEnum
from typing import Iterable, Mapping, Sequence

MAX_SOFT_SIGNALS = 32
MAX_PROVENANCE_POINTERS = 8
MAX_ELIGIBILITY_REASONS = 8
RULE_VERSION = "cooking-recommendation-state-v1"


class SubjectType(str, Enum):
    CANDIDATE = "candidate"
    DISH = "dish"
    LANE = "lane"
    LEARNING = "learning"
    PREFERENCE = "preference"
    PREREQUISITE = "prerequisite"


class SignalType(str, Enum):
    COOKED = "cooked"
    REPEAT = "repeat"
    CORRECTION = "correction"
    SATURATION = "saturation"
    BLOCKER = "blocker"
    SUPPRESSION = "suppression"
    EXCLUSION = "exclusion"
    LANE = "lane"
    LEARNING = "learning"
    INTEREST = "interest"
    PREFERENCE = "preference"
    EFFORT = "effort"
    FORMAT = "format"
    NOVELTY = "novelty"
    PREREQUISITE = "prerequisite"


class ScopeKind(str, Enum):
    REQUEST = "request"
    SESSION = "session"
    CANDIDATE = "candidate"
    DISH = "dish"
    LANE = "lane"
    LEARNING = "learning"
    PREFERENCE = "preference"
    GLOBAL = "global"


class Strength(IntEnum):
    WEAK = 1
    MEDIUM = 2
    STRONG = 3


class Confidence(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class Lifetime(str, Enum):
    REQUEST = "request"
    SESSION = "session"
    MEDIUM_TERM = "medium_term"
    UNTIL_WAKE = "until_wake"
    UNTIL_EXPIRY = "until_expiry"
    DURABLE = "durable"


class EvidenceSource(str, Enum):
    EXPLICIT_MARCO = "explicit_marco"
    AGENT_INFERENCE = "agent_inference"
    RUNTIME_FACT = "runtime_fact"
    COOK_OUTCOME = "cook_outcome"
    IMPORTED_HISTORY = "imported_history"
    SCRATCHPAD_AUTHORITY = "scratchpad_authority"
    PROFILE_AUTHORITY = "profile_authority"


class EvidenceKind(IntEnum):
    DERIVED = 1
    AUTHORITATIVE = 2
    EXPLICIT = 3


class EligibilityEffect(str, Enum):
    SOFT = "soft"
    HARD_BLOCK = "hard_block"
    HARD_EXCLUDE = "hard_exclude"


class EventKind(str, Enum):
    SET = "set"
    CLEAR = "clear"


class CandidateEligibility(str, Enum):
    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    EXCLUDED = "excluded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SignalScope:
    kind: ScopeKind
    key: str

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("scope key must be non-blank")


@dataclass(frozen=True)
class RecommendationEvidence:
    event_id: str
    kind: EventKind
    subject_type: SubjectType
    subject_key: str
    signal_type: SignalType
    value: str
    scope: SignalScope
    strength: Strength
    confidence: Confidence
    lifetime: Lifetime
    valid_from: datetime
    source: EvidenceSource
    evidence_kind: EvidenceKind
    provenance: tuple[str, ...]
    eligibility_effect: EligibilityEffect = EligibilityEffect.SOFT
    expires_at: datetime | None = None
    wake_condition: str | None = None
    supersedes: tuple[str, ...] = ()
    clears: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must be non-blank")
        if not self.subject_key.strip():
            raise ValueError("subject_key must be non-blank")
        if self.kind is EventKind.SET and not self.value.strip():
            raise ValueError("set events require a non-blank value")
        if not self.provenance or any(not item.strip() for item in self.provenance):
            raise ValueError("at least one immutable provenance pointer is required")
        _require_aware(self.valid_from, "valid_from")
        if self.expires_at is not None:
            _require_aware(self.expires_at, "expires_at")
        if self.lifetime is Lifetime.UNTIL_EXPIRY and self.expires_at is None:
            raise ValueError("until_expiry evidence requires expires_at")
        if self.expires_at is not None and self.lifetime is not Lifetime.UNTIL_EXPIRY:
            raise ValueError("expires_at is only valid for until_expiry evidence")
        if self.lifetime is Lifetime.UNTIL_WAKE and not (self.wake_condition or "").strip():
            raise ValueError("until_wake evidence requires a wake_condition")
        if self.wake_condition is not None and self.lifetime is not Lifetime.UNTIL_WAKE:
            raise ValueError("wake_condition is only valid for until_wake evidence")
        if self.kind is EventKind.CLEAR:
            if not self.clears:
                raise ValueError("clear events must name at least one signal to clear")
            if self.eligibility_effect is not EligibilityEffect.SOFT:
                raise ValueError("clear events do not carry eligibility effects")
        if self.evidence_kind is EvidenceKind.DERIVED and (self.clears or self.supersedes):
            raise ValueError("derived evidence cannot clear or supersede authoritative state")
        explicit_sources = {
            EvidenceSource.EXPLICIT_MARCO,
            EvidenceSource.SCRATCHPAD_AUTHORITY,
            EvidenceSource.PROFILE_AUTHORITY,
        }
        if self.evidence_kind is EvidenceKind.EXPLICIT and self.source not in explicit_sources:
            raise ValueError("explicit evidence requires an explicit source authority")
        if self.source is EvidenceSource.AGENT_INFERENCE and self.evidence_kind is not EvidenceKind.DERIVED:
            raise ValueError("agent inference must use derived evidence")
        lifecycle_sources = {
            EvidenceSource.EXPLICIT_MARCO,
            EvidenceSource.RUNTIME_FACT,
            EvidenceSource.SCRATCHPAD_AUTHORITY,
            EvidenceSource.PROFILE_AUTHORITY,
        }
        if (self.clears or self.supersedes) and self.source not in lifecycle_sources:
            raise ValueError("lifecycle relations require an authoritative source")
        _validate_eligibility_effect(self)


@dataclass(frozen=True)
class RecommendationSignal:
    signal_id: str
    subject_type: SubjectType
    subject_key: str
    signal_type: SignalType
    value: str
    scope: SignalScope
    strength: Strength
    confidence: Confidence
    lifetime: Lifetime
    valid_from: datetime
    source: EvidenceSource
    evidence_kind: EvidenceKind
    provenance: tuple[str, ...]
    eligibility_effect: EligibilityEffect
    expires_at: datetime | None
    wake_condition: str | None
    reason: str | None

    @property
    def slot(self) -> tuple[SubjectType, str, SignalType, ScopeKind, str, EligibilityEffect]:
        return (
            self.subject_type,
            self.subject_key,
            self.signal_type,
            self.scope.kind,
            self.scope.key,
            self.eligibility_effect,
        )


@dataclass(frozen=True)
class CompiledRecommendationState:
    rule_version: str
    as_of: datetime
    signals: tuple[RecommendationSignal, ...]
    inactive_signal_ids: tuple[str, ...]

    def for_subject(
        self, subject_type: SubjectType, subject_key: str
    ) -> tuple[RecommendationSignal, ...]:
        return tuple(
            signal
            for signal in self.signals
            if signal.subject_type is subject_type and signal.subject_key == subject_key
        )


@dataclass(frozen=True)
class EligibilityObservation:
    status: CandidateEligibility
    provenance: tuple[str, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is CandidateEligibility.UNKNOWN:
            raise ValueError("unknown is represented by a missing authoritative observation")
        if not self.provenance or any(not item.strip() for item in self.provenance):
            raise ValueError("eligibility observations require provenance")


@dataclass(frozen=True)
class CandidateEligibilityResult:
    candidate_key: str
    status: CandidateEligibility
    reasons: tuple[str, ...]
    provenance: tuple[str, ...]

    @property
    def actionable(self) -> bool:
        return self.status is CandidateEligibility.ELIGIBLE


@dataclass(frozen=True)
class RecommendationContext:
    rule_version: str
    soft_signals: tuple[RecommendationSignal, ...]
    eligibility: tuple[CandidateEligibilityResult, ...]

    def eligibility_for(self, candidate_key: str) -> CandidateEligibilityResult:
        for item in self.eligibility:
            if item.candidate_key == candidate_key:
                return item
        raise KeyError(candidate_key)


def compile_recommendation_state(
    events: Iterable[RecommendationEvidence],
    *,
    as_of: datetime,
    ended_scopes: Iterable[SignalScope] = (),
    resolved_wake_conditions: Iterable[str] = (),
) -> CompiledRecommendationState:
    """Compile current state without implicit time decay.

    State changes only through an explicit scope end, explicit expiry, authoritative
    wake resolution, clear/supersession evidence, or later evidence in the same
    logical slot. Weaker evidence cannot erase stronger state implicitly.
    """
    _require_aware(as_of, "as_of")
    ended = set(ended_scopes)
    resolved_wakes = {item for item in resolved_wake_conditions if item}
    ordered = sorted(events, key=lambda item: (item.valid_from, item.event_id))
    event_ids = [event.event_id for event in ordered]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("recommendation evidence event_id values must be unique")

    active: dict[str, RecommendationSignal] = {}
    inactive: set[str] = set()

    for event in ordered:
        if event.valid_from > as_of:
            continue

        # Explicit lifecycle relations are deterministic and append-only. Cross-
        # source retirement is allowed only for a narrow accepted authority
        # transition; unrelated proper authorities remain isolated.
        for target_id in (*event.supersedes, *event.clears):
            target = active.get(target_id)
            if target is not None:
                _validate_lifecycle_relation(event, target)
                del active[target_id]
            inactive.add(target_id)

        if event.kind is EventKind.CLEAR:
            inactive.add(event.event_id)
            continue

        signal = _signal_from_event(event)

        # Repeated current state in one logical slot is folded only when the new
        # event is allowed to retire the prior signal. Hard eligibility uses its
        # lifecycle identity as an additional same-fact boundary so one genuine
        # blocker cannot erase another merely because their coarse slot matches.
        # Unrelated source authorities remain visible as conflicts. Weaker same-
        # source evidence cannot silently erase stronger state.
        folded_into_stronger = False
        for signal_id, previous in tuple(active.items()):
            if previous.slot != signal.slot:
                continue
            if not _same_hard_lifecycle_fact(previous, signal):
                continue
            can_retire = _source_can_retire(event, previous)
            if not can_retire:
                if previous.value == signal.value and _signal_can_absorb_same_value(
                    previous, event
                ):
                    active[signal_id] = replace(
                        previous,
                        provenance=_merge_provenance(previous.provenance, signal.provenance),
                    )
                    inactive.add(signal.signal_id)
                    folded_into_stronger = True
                    break
                continue
            if (
                event.evidence_kind < previous.evidence_kind
                and not _controlling_authority_override(event, previous)
            ):
                if previous.value == signal.value:
                    active[signal_id] = replace(
                        previous,
                        provenance=_merge_provenance(previous.provenance, signal.provenance),
                    )
                    inactive.add(signal.signal_id)
                    folded_into_stronger = True
                    break
                continue
            if previous.value == signal.value:
                signal = replace(
                    signal,
                    provenance=_merge_provenance(previous.provenance, signal.provenance),
                )
            del active[signal_id]
            inactive.add(signal_id)
        if folded_into_stronger:
            continue

        if _is_inactive_by_lifecycle(
            signal,
            as_of=as_of,
            ended_scopes=ended,
            resolved_wake_conditions=resolved_wakes,
        ):
            inactive.add(signal.signal_id)
            continue

        active[signal.signal_id] = signal

    # A later wake/expiry/scope resolution also applies to signals inserted
    # before the resolving input was supplied.
    for signal_id, signal in tuple(active.items()):
        if _is_inactive_by_lifecycle(
            signal,
            as_of=as_of,
            ended_scopes=ended,
            resolved_wake_conditions=resolved_wakes,
        ):
            del active[signal_id]
            inactive.add(signal_id)

    return CompiledRecommendationState(
        rule_version=RULE_VERSION,
        as_of=as_of,
        signals=tuple(sorted(active.values(), key=lambda signal: signal.signal_id)),
        inactive_signal_ids=tuple(sorted(inactive)),
    )


def build_recommendation_context(
    state: CompiledRecommendationState,
    *,
    candidate_keys: Sequence[str],
    authoritative_eligibility: Mapping[str, EligibilityObservation] | None = None,
    relevant_subjects: Mapping[SubjectType, Iterable[str]] | None = None,
    soft_limit: int = MAX_SOFT_SIGNALS,
) -> RecommendationContext:
    """Build the bounded prompt/read-model payload for a candidate set.

    Hard eligibility is resolved per candidate from the complete active state
    plus authoritative observations; it is never truncated by ``soft_limit``.
    A candidate with no exact eligibility observation and no known active hard
    signal is UNKNOWN and therefore non-actionable.

    Candidate/dish state is matched automatically by candidate key. Lane,
    learning, and prerequisite state is included only when the caller declares
    that subject relevant to the candidate set. Broader preference state remains
    eligible by default and may be narrowed through ``relevant_subjects``.
    """
    if soft_limit < 0 or soft_limit > MAX_SOFT_SIGNALS:
        raise ValueError(f"soft_limit must be between 0 and {MAX_SOFT_SIGNALS}")

    candidates = tuple(dict.fromkeys(key.strip() for key in candidate_keys if key.strip()))
    observations = authoritative_eligibility or {}
    subject_filters = {
        subject_type: {key.strip() for key in keys if key.strip()}
        for subject_type, keys in (relevant_subjects or {}).items()
    }

    eligibility = tuple(
        _candidate_eligibility(state, candidate_key, observations.get(candidate_key))
        for candidate_key in candidates
    )

    candidate_set = set(candidates)
    soft = [
        signal
        for signal in state.signals
        if signal.eligibility_effect is EligibilityEffect.SOFT
        and _soft_signal_relevant(signal, candidate_set, subject_filters)
    ]
    soft.sort(key=_soft_priority_key)

    return RecommendationContext(
        rule_version=state.rule_version,
        soft_signals=tuple(soft[:soft_limit]),
        eligibility=eligibility,
    )


def _candidate_eligibility(
    state: CompiledRecommendationState,
    candidate_key: str,
    observation: EligibilityObservation | None,
) -> CandidateEligibilityResult:
    hard_signals = tuple(
        signal
        for signal in state.signals
        if signal.subject_type in {SubjectType.CANDIDATE, SubjectType.DISH}
        and signal.subject_key == candidate_key
        and signal.eligibility_effect is not EligibilityEffect.SOFT
    )

    local_exclusions = tuple(
        signal
        for signal in hard_signals
        if signal.eligibility_effect is EligibilityEffect.HARD_EXCLUDE
    )
    local_blocks = tuple(
        signal
        for signal in hard_signals
        if signal.eligibility_effect is EligibilityEffect.HARD_BLOCK
    )

    # External proper-authority blocks/exclusions are exact facts and cannot
    # be softened by this recommendation layer.
    if observation is not None and observation.status is CandidateEligibility.EXCLUDED:
        return _eligibility_result(
            candidate_key,
            CandidateEligibility.EXCLUDED,
            local_exclusions,
            observation,
        )
    if local_exclusions:
        return _eligibility_result(
            candidate_key,
            CandidateEligibility.EXCLUDED,
            local_exclusions,
            observation,
        )
    if observation is not None and observation.status is CandidateEligibility.BLOCKED:
        return _eligibility_result(
            candidate_key,
            CandidateEligibility.BLOCKED,
            local_blocks,
            observation,
        )
    if local_blocks:
        return _eligibility_result(
            candidate_key,
            CandidateEligibility.BLOCKED,
            local_blocks,
            observation,
        )
    if observation is not None and observation.status is CandidateEligibility.ELIGIBLE:
        return _eligibility_result(
            candidate_key,
            CandidateEligibility.ELIGIBLE,
            (),
            observation,
        )

    return CandidateEligibilityResult(
        candidate_key=candidate_key,
        status=CandidateEligibility.UNKNOWN,
        reasons=("authoritative eligibility not refreshed",),
        provenance=(),
    )


def _eligibility_result(
    candidate_key: str,
    status: CandidateEligibility,
    local_signals: Sequence[RecommendationSignal],
    observation: EligibilityObservation | None,
) -> CandidateEligibilityResult:
    reasons = [signal.reason or signal.value for signal in local_signals]
    provenance = [pointer for signal in local_signals for pointer in signal.provenance]
    if observation is not None and observation.status is status:
        if observation.reason:
            reasons.append(observation.reason)
        provenance.extend(observation.provenance)
    return CandidateEligibilityResult(
        candidate_key=candidate_key,
        status=status,
        reasons=_bounded_unique(reasons, MAX_ELIGIBILITY_REASONS),
        provenance=_bounded_unique(provenance, MAX_PROVENANCE_POINTERS),
    )


def _signal_from_event(event: RecommendationEvidence) -> RecommendationSignal:
    return RecommendationSignal(
        signal_id=event.event_id,
        subject_type=event.subject_type,
        subject_key=event.subject_key,
        signal_type=event.signal_type,
        value=event.value,
        scope=event.scope,
        strength=event.strength,
        confidence=event.confidence,
        lifetime=event.lifetime,
        valid_from=event.valid_from,
        source=event.source,
        evidence_kind=event.evidence_kind,
        provenance=_bounded_unique(event.provenance, MAX_PROVENANCE_POINTERS),
        eligibility_effect=event.eligibility_effect,
        expires_at=event.expires_at,
        wake_condition=event.wake_condition,
        reason=event.reason,
    )


def _same_hard_lifecycle_fact(
    previous: RecommendationSignal,
    current: RecommendationSignal,
) -> bool:
    """Return whether coarse same-slot state is safe to coalesce implicitly.

    Soft recommendation state keeps the established slot behavior. Hard eligibility
    is fail-closed: only signals with the same value and lifecycle fate are treated
    as repeated observations of one fact. Distinct hard facts require an explicit
    clear/supersedes relation to retire one another.
    """
    if current.eligibility_effect is EligibilityEffect.SOFT:
        return True
    return (
        previous.value == current.value
        and previous.lifetime is current.lifetime
        and previous.expires_at == current.expires_at
        and previous.wake_condition == current.wake_condition
        and previous.reason == current.reason
    )


def _validate_lifecycle_relation(
    event: RecommendationEvidence,
    target: RecommendationSignal,
) -> None:
    if (
        event.subject_type is not target.subject_type
        or event.subject_key != target.subject_key
        or event.scope != target.scope
    ):
        raise ValueError("lifecycle relation must match target subject and scope")
    if (
        event.evidence_kind < target.evidence_kind
        and not _controlling_authority_override(event, target)
    ):
        raise ValueError("lifecycle relation cannot retire stronger evidence")
    if not _source_can_retire(event, target):
        raise ValueError("lifecycle relation cannot cross source authority")


def _source_can_retire(
    event: RecommendationEvidence,
    target: RecommendationSignal,
) -> bool:
    return _source_authority_can_control(
        controller_source=event.source,
        controlled_source=target.source,
        subject_type=event.subject_type,
        signal_type=event.signal_type,
        target_signal_type=target.signal_type,
    )


def _signal_can_absorb_same_value(
    signal: RecommendationSignal,
    event: RecommendationEvidence,
) -> bool:
    if not _source_authority_can_control(
        controller_source=signal.source,
        controlled_source=event.source,
        subject_type=signal.subject_type,
        signal_type=signal.signal_type,
        target_signal_type=event.signal_type,
    ):
        return False
    return (
        signal.evidence_kind >= event.evidence_kind
        or _source_owner_override(
            controller_source=signal.source,
            controlled_source=event.source,
            subject_type=signal.subject_type,
            signal_type=signal.signal_type,
        )
    )


def _source_authority_can_control(
    *,
    controller_source: EvidenceSource,
    controlled_source: EvidenceSource,
    subject_type: SubjectType,
    signal_type: SignalType,
    target_signal_type: SignalType,
) -> bool:
    if controller_source is controlled_source:
        return True
    if signal_type is not target_signal_type:
        return False

    # Proper authorities may replace lower-authority recommendation evidence for
    # the exact same fact, but may not erase one another's independently owned
    # state.
    lower_authority_sources = {
        EvidenceSource.AGENT_INFERENCE,
        EvidenceSource.COOK_OUTCOME,
        EvidenceSource.IMPORTED_HISTORY,
    }
    proper_authority_sources = {
        EvidenceSource.EXPLICIT_MARCO,
        EvidenceSource.RUNTIME_FACT,
        EvidenceSource.SCRATCHPAD_AUTHORITY,
        EvidenceSource.PROFILE_AUTHORITY,
    }
    if controlled_source in lower_authority_sources and controller_source in proper_authority_sources:
        return True

    return _source_owner_override(
        controller_source=controller_source,
        controlled_source=controlled_source,
        subject_type=subject_type,
        signal_type=signal_type,
    )


def _source_owner_override(
    *,
    controller_source: EvidenceSource,
    controlled_source: EvidenceSource,
    subject_type: SubjectType,
    signal_type: SignalType,
) -> bool:
    # Once stable configurable preference state exists in Profile, it controls
    # that fact over direct recommendation evidence without making Profile a
    # generic authority over runtime or Scratchpad state.
    if (
        controller_source is EvidenceSource.PROFILE_AUTHORITY
        and controlled_source is EvidenceSource.EXPLICIT_MARCO
        and subject_type is SubjectType.PREFERENCE
    ):
        return True

    return False


def _controlling_authority_override(
    event: RecommendationEvidence,
    target: RecommendationSignal,
) -> bool:
    return _source_owner_override(
        controller_source=event.source,
        controlled_source=target.source,
        subject_type=event.subject_type,
        signal_type=event.signal_type,
    )


def _is_inactive_by_lifecycle(
    signal: RecommendationSignal,
    *,
    as_of: datetime,
    ended_scopes: set[SignalScope],
    resolved_wake_conditions: set[str],
) -> bool:
    if signal.scope in ended_scopes:
        return True
    if signal.lifetime is Lifetime.UNTIL_EXPIRY:
        assert signal.expires_at is not None
        if as_of >= signal.expires_at:
            return True
    if signal.lifetime is Lifetime.UNTIL_WAKE:
        assert signal.wake_condition is not None
        if signal.wake_condition in resolved_wake_conditions:
            return True
    return False


def _soft_signal_relevant(
    signal: RecommendationSignal,
    candidate_keys: set[str],
    relevant_subjects: Mapping[SubjectType, set[str]],
) -> bool:
    if signal.subject_type in {SubjectType.CANDIDATE, SubjectType.DISH}:
        return signal.subject_key in candidate_keys
    if signal.subject_type in {SubjectType.LANE, SubjectType.LEARNING, SubjectType.PREREQUISITE}:
        return signal.subject_key in relevant_subjects.get(signal.subject_type, set())
    if signal.subject_type is SubjectType.PREFERENCE:
        allowed = relevant_subjects.get(SubjectType.PREFERENCE)
        return allowed is None or signal.subject_key in allowed
    return signal.scope.kind in {ScopeKind.REQUEST, ScopeKind.SESSION}


def _soft_priority_key(signal: RecommendationSignal) -> tuple[int, int, int, int, int, float, str]:
    tier = _scope_tier(signal)
    # A current durable source owner wins for the fact it owns; otherwise the
    # accepted direct-evidence/confidence/strength ordering applies.
    authority_priority = _controlling_source_priority(signal)
    # Lower tuple sorts first. Later timestamps sort first by negating POSIX
    # time; stable identity is the final deterministic tie-break.
    return (
        tier,
        authority_priority,
        -int(signal.evidence_kind),
        -int(signal.confidence),
        -int(signal.strength),
        -signal.valid_from.timestamp(),
        signal.signal_id,
    )


def _controlling_source_priority(signal: RecommendationSignal) -> int:
    if (
        signal.source is EvidenceSource.PROFILE_AUTHORITY
        and signal.subject_type is SubjectType.PREFERENCE
    ):
        return 0
    return 1


def _scope_tier(signal: RecommendationSignal) -> int:
    if signal.scope.kind in {ScopeKind.REQUEST, ScopeKind.SESSION}:
        return 0
    if signal.scope.kind in {ScopeKind.CANDIDATE, ScopeKind.DISH} or signal.subject_type in {
        SubjectType.CANDIDATE,
        SubjectType.DISH,
    }:
        return 1
    if signal.scope.kind in {ScopeKind.LANE, ScopeKind.LEARNING} or signal.subject_type in {
        SubjectType.LANE,
        SubjectType.LEARNING,
    }:
        return 2
    return 3


def _validate_eligibility_effect(event: RecommendationEvidence) -> None:
    if event.kind is EventKind.SET:
        if event.signal_type is SignalType.BLOCKER and event.eligibility_effect is not EligibilityEffect.HARD_BLOCK:
            raise ValueError("blocker signals must be hard blocks")
        if event.signal_type is SignalType.EXCLUSION and event.eligibility_effect is not EligibilityEffect.HARD_EXCLUDE:
            raise ValueError("exclusion signals must be hard exclusions")

    if event.eligibility_effect is EligibilityEffect.SOFT:
        return
    if event.kind is not EventKind.SET:
        raise ValueError("only set events can carry hard eligibility")
    if event.subject_type not in {SubjectType.CANDIDATE, SubjectType.DISH}:
        raise ValueError("hard eligibility is candidate/dish scoped")
    if event.evidence_kind is EvidenceKind.DERIVED or event.source is EvidenceSource.AGENT_INFERENCE:
        raise ValueError("inference cannot create hard eligibility")
    trusted = {
        EvidenceSource.EXPLICIT_MARCO,
        EvidenceSource.RUNTIME_FACT,
        EvidenceSource.SCRATCHPAD_AUTHORITY,
        EvidenceSource.PROFILE_AUTHORITY,
    }
    if event.source not in trusted:
        raise ValueError("hard eligibility requires an authoritative source")
    if event.eligibility_effect is EligibilityEffect.HARD_BLOCK:
        if event.signal_type not in {SignalType.BLOCKER, SignalType.PREREQUISITE}:
            raise ValueError("hard blocks require blocker/prerequisite signal type")
        return
    if event.signal_type is not SignalType.EXCLUSION:
        raise ValueError("hard exclusions require exclusion signal type")
    if event.evidence_kind is not EvidenceKind.EXPLICIT:
        raise ValueError("hard exclusions require explicit evidence")
    if event.lifetime is not Lifetime.DURABLE:
        raise ValueError("hard exclusions must be explicitly durable")


def _bounded_unique(values: Iterable[str], limit: int) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(values))
    if len(unique) <= limit:
        return unique
    return unique[-limit:]


def _merge_provenance(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return _bounded_unique(
        (pointer for group in groups for pointer in group),
        MAX_PROVENANCE_POINTERS,
    )


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
