from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dish_tool.recommendation_state import (
    MAX_SOFT_SIGNALS,
    CandidateEligibility,
    Confidence,
    EligibilityEffect,
    EligibilityObservation,
    EventKind,
    EvidenceKind,
    EvidenceSource,
    Lifetime,
    RecommendationEvidence,
    ScopeKind,
    SignalScope,
    SignalType,
    Strength,
    SubjectType,
    build_recommendation_context,
    compile_recommendation_state,
)


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SESSION = SignalScope(ScopeKind.SESSION, "session-1")
GLOBAL = SignalScope(ScopeKind.GLOBAL, "global")


def ev(
    event_id: str,
    *,
    subject: str,
    signal_type: SignalType,
    value: str,
    at: datetime = T0,
    subject_type: SubjectType = SubjectType.DISH,
    scope: SignalScope | None = None,
    lifetime: Lifetime = Lifetime.DURABLE,
    source: EvidenceSource = EvidenceSource.EXPLICIT_MARCO,
    evidence_kind: EvidenceKind = EvidenceKind.EXPLICIT,
    strength: Strength = Strength.MEDIUM,
    confidence: Confidence = Confidence.MEDIUM,
    eligibility: EligibilityEffect = EligibilityEffect.SOFT,
    expires_at: datetime | None = None,
    wake: str | None = None,
    supersedes: tuple[str, ...] = (),
    clears: tuple[str, ...] = (),
    reason: str | None = None,
) -> RecommendationEvidence:
    return RecommendationEvidence(
        event_id=event_id,
        kind=EventKind.SET,
        subject_type=subject_type,
        subject_key=subject,
        signal_type=signal_type,
        value=value,
        scope=scope or SignalScope(ScopeKind.DISH, subject),
        strength=strength,
        confidence=confidence,
        lifetime=lifetime,
        valid_from=at,
        source=source,
        evidence_kind=evidence_kind,
        provenance=(f"evidence:{event_id}",),
        eligibility_effect=eligibility,
        expires_at=expires_at,
        wake_condition=wake,
        supersedes=supersedes,
        clears=clears,
        reason=reason,
    )


def eligible(*names: str) -> dict[str, EligibilityObservation]:
    return {
        name: EligibilityObservation(
            CandidateEligibility.ELIGIBLE, (f"eligibility:{name}",)
        )
        for name in names
    }


def test_cooked_history_is_soft_state_and_not_a_hard_filter() -> None:
    state = compile_recommendation_state(
        [
            ev(
                "soto-cooked",
                subject="soto ayam",
                signal_type=SignalType.COOKED,
                value="recent",
                source=EvidenceSource.IMPORTED_HISTORY,
                evidence_kind=EvidenceKind.AUTHORITATIVE,
            )
        ],
        as_of=T0,
    )
    context = build_recommendation_context(
        state,
        candidate_keys=["soto ayam"],
        authoritative_eligibility=eligible("soto ayam"),
    )
    assert context.eligibility_for("soto ayam").actionable
    assert [(s.signal_type, s.value) for s in context.soft_signals] == [
        (SignalType.COOKED, "recent")
    ]


def test_blocker_wakes_without_erasing_evidence_identity() -> None:
    blocked = ev(
        "rendang-blocked",
        subject="rendang",
        signal_type=SignalType.BLOCKER,
        value="missing kerisik",
        lifetime=Lifetime.UNTIL_WAKE,
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        eligibility=EligibilityEffect.HARD_BLOCK,
        wake="kerisik-arrived",
    )
    before = compile_recommendation_state([blocked], as_of=T0)
    after = compile_recommendation_state(
        [blocked],
        as_of=T0 + timedelta(days=5),
        resolved_wake_conditions={"kerisik-arrived"},
    )
    assert (
        build_recommendation_context(before, candidate_keys=["rendang"])
        .eligibility_for("rendang")
        .status
        is CandidateEligibility.BLOCKED
    )
    assert after.signals == ()
    assert "rendang-blocked" in after.inactive_signal_ids


def test_runtime_sourcing_blocker_is_hard_and_never_spends_soft_budget() -> None:
    blocker = ev(
        "zhong-blocked",
        subject="zhong shui jiao",
        signal_type=SignalType.PREREQUISITE,
        value="chili oil unavailable",
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        eligibility=EligibilityEffect.HARD_BLOCK,
    )
    state = compile_recommendation_state([blocker], as_of=T0)
    context = build_recommendation_context(state, candidate_keys=["zhong shui jiao"])
    assert context.eligibility_for("zhong shui jiao").status is CandidateEligibility.BLOCKED
    assert context.soft_signals == ()


def test_lane_and_interest_are_soft_ranking_evidence_when_lane_matches() -> None:
    state = compile_recommendation_state(
        [
            ev(
                "jalfrezi-lane",
                subject="curries",
                subject_type=SubjectType.LANE,
                signal_type=SignalType.LANE,
                value="consolidate",
                scope=SignalScope(ScopeKind.LANE, "curries"),
                lifetime=Lifetime.MEDIUM_TERM,
            ),
            ev(
                "fesenjan-interest",
                subject="fesenjan",
                signal_type=SignalType.INTEREST,
                value="high",
                strength=Strength.STRONG,
            ),
        ],
        as_of=T0,
    )
    context = build_recommendation_context(
        state,
        candidate_keys=["jalfrezi", "fesenjan"],
        authoritative_eligibility=eligible("jalfrezi", "fesenjan"),
        relevant_subjects={SubjectType.LANE: {"curries"}},
    )
    assert {s.signal_id for s in context.soft_signals} == {
        "jalfrezi-lane",
        "fesenjan-interest",
    }


def test_nonmatching_lane_does_not_consume_bounded_payload() -> None:
    state = compile_recommendation_state(
        [
            ev(
                "curries",
                subject="curries",
                subject_type=SubjectType.LANE,
                signal_type=SignalType.LANE,
                value="consolidate",
                scope=SignalScope(ScopeKind.LANE, "curries"),
            ),
            ev(
                "baking",
                subject="baking",
                subject_type=SubjectType.LANE,
                signal_type=SignalType.LANE,
                value="expand",
                scope=SignalScope(ScopeKind.LANE, "baking"),
            ),
        ],
        as_of=T0,
    )
    context = build_recommendation_context(
        state,
        candidate_keys=["jalfrezi"],
        authoritative_eligibility=eligible("jalfrezi"),
        relevant_subjects={SubjectType.LANE: {"curries"}},
    )
    assert [s.signal_id for s in context.soft_signals] == ["curries"]


def test_session_scoped_unrelated_dish_does_not_consume_payload() -> None:
    state = compile_recommendation_state(
        [
            ev(
                "not-risotto",
                subject="risotto",
                signal_type=SignalType.SUPPRESSION,
                value="not tonight",
                scope=SESSION,
                lifetime=Lifetime.SESSION,
            )
        ],
        as_of=T0,
    )
    context = build_recommendation_context(
        state,
        candidate_keys=["fesenjan"],
        authoritative_eligibility=eligible("fesenjan"),
    )
    assert context.soft_signals == ()


def test_dish_saturation_does_not_generalize_to_cuisine() -> None:
    state = compile_recommendation_state(
        [
            ev(
                "norma-saturated",
                subject="pasta alla norma",
                signal_type=SignalType.SATURATION,
                value="saturated",
            )
        ],
        as_of=T0,
    )
    context = build_recommendation_context(
        state,
        candidate_keys=["pasta alla norma", "amatriciana"],
        authoritative_eligibility=eligible("pasta alla norma", "amatriciana"),
    )
    assert [s.subject_key for s in context.soft_signals] == ["pasta alla norma"]
    assert context.eligibility_for("amatriciana").actionable


def test_session_scope_ends_but_explicit_durable_exclusion_persists() -> None:
    not_tonight = ev(
        "not-tonight",
        subject="mushroom risotto",
        signal_type=SignalType.SUPPRESSION,
        value="not tonight",
        scope=SESSION,
        lifetime=Lifetime.SESSION,
    )
    durable = ev(
        "durable-exclude",
        subject="sweet breakfast",
        signal_type=SignalType.EXCLUSION,
        value="do not recommend",
        lifetime=Lifetime.DURABLE,
        source=EvidenceSource.EXPLICIT_MARCO,
        evidence_kind=EvidenceKind.EXPLICIT,
        eligibility=EligibilityEffect.HARD_EXCLUDE,
    )
    state = compile_recommendation_state(
        [not_tonight, durable],
        as_of=T0 + timedelta(days=1),
        ended_scopes={SESSION},
    )
    assert {s.signal_id for s in state.signals} == {"durable-exclude"}
    context = build_recommendation_context(state, candidate_keys=["sweet breakfast"])
    assert context.eligibility_for("sweet breakfast").status is CandidateEligibility.EXCLUDED


def test_positive_interest_and_correction_can_coexist() -> None:
    state = compile_recommendation_state(
        [
            ev(
                "great",
                subject="basque cheesecake",
                signal_type=SignalType.INTEREST,
                value="positive",
            ),
            ev(
                "sweet",
                subject="basque cheesecake",
                signal_type=SignalType.CORRECTION,
                value="too sweet",
            ),
        ],
        as_of=T0,
    )
    assert {s.signal_type for s in state.signals} == {
        SignalType.INTEREST,
        SignalType.CORRECTION,
    }


def test_inference_cannot_harden_into_block_or_durable_exclusion() -> None:
    with pytest.raises(ValueError, match="inference cannot create hard eligibility"):
        ev(
            "bad-inference",
            subject="candidate",
            signal_type=SignalType.BLOCKER,
            value="maybe not",
            source=EvidenceSource.AGENT_INFERENCE,
            evidence_kind=EvidenceKind.DERIVED,
            eligibility=EligibilityEffect.HARD_BLOCK,
        )
    with pytest.raises(ValueError, match="hard exclusions require explicit evidence"):
        ev(
            "bad-exclusion",
            subject="candidate",
            signal_type=SignalType.EXCLUSION,
            value="exclude",
            source=EvidenceSource.PROFILE_AUTHORITY,
            evidence_kind=EvidenceKind.AUTHORITATIVE,
            eligibility=EligibilityEffect.HARD_EXCLUDE,
        )


def test_blocker_and_exclusion_labels_cannot_be_softened() -> None:
    with pytest.raises(ValueError, match="blocker signals must be hard blocks"):
        ev(
            "soft-blocker",
            subject="candidate",
            signal_type=SignalType.BLOCKER,
            value="maybe blocked",
        )
    with pytest.raises(ValueError, match="exclusion signals must be hard exclusions"):
        ev(
            "soft-exclusion",
            subject="candidate",
            signal_type=SignalType.EXCLUSION,
            value="maybe exclude",
        )


def test_narrow_session_signal_precedes_conflicting_broad_preference_without_rewriting_it() -> None:
    state = compile_recommendation_state(
        [
            ev(
                "broad-spicy",
                subject="spice",
                subject_type=SubjectType.PREFERENCE,
                signal_type=SignalType.PREFERENCE,
                value="likes spicy",
                scope=GLOBAL,
            ),
            ev(
                "session-mild",
                subject="spice",
                subject_type=SubjectType.PREFERENCE,
                signal_type=SignalType.PREFERENCE,
                value="mild tonight",
                scope=SESSION,
                lifetime=Lifetime.SESSION,
                at=T0 + timedelta(minutes=1),
            ),
        ],
        as_of=T0 + timedelta(minutes=2),
    )
    context = build_recommendation_context(
        state,
        candidate_keys=["laksa"],
        authoritative_eligibility=eligible("laksa"),
    )
    assert [s.signal_id for s in context.soft_signals] == ["session-mild", "broad-spicy"]


def test_soft_payload_is_capped_but_hard_lookup_reads_complete_state() -> None:
    soft = [
        ev(
            f"soft-{index:05d}",
            subject=f"preference-{index}",
            subject_type=SubjectType.PREFERENCE,
            signal_type=SignalType.INTEREST,
            value="curious",
            at=T0 + timedelta(seconds=index),
            scope=GLOBAL,
            source=EvidenceSource.AGENT_INFERENCE,
            evidence_kind=EvidenceKind.DERIVED,
        )
        for index in range(10_000)
    ]
    hard = ev(
        "zz-hard-beyond-cap",
        subject="blocked dish",
        signal_type=SignalType.BLOCKER,
        value="ingredient unavailable",
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        eligibility=EligibilityEffect.HARD_BLOCK,
    )
    state = compile_recommendation_state([*soft, hard], as_of=T0 + timedelta(days=1))
    context = build_recommendation_context(state, candidate_keys=["blocked dish"])
    assert len(context.soft_signals) == MAX_SOFT_SIGNALS
    assert context.eligibility_for("blocked dish").status is CandidateEligibility.BLOCKED


def test_missing_exact_eligibility_is_unknown_and_nonactionable() -> None:
    state = compile_recommendation_state([], as_of=T0)
    result = build_recommendation_context(state, candidate_keys=["unknown"]).eligibility_for(
        "unknown"
    )
    assert result.status is CandidateEligibility.UNKNOWN
    assert not result.actionable


def test_later_explicit_same_scope_state_supersedes_earlier_state() -> None:
    old = ev(
        "old-correction",
        subject="tagine",
        signal_type=SignalType.CORRECTION,
        value="too sweet",
    )
    new = ev(
        "new-correction",
        subject="tagine",
        signal_type=SignalType.CORRECTION,
        value="balanced now",
        at=T0 + timedelta(days=1),
    )
    state = compile_recommendation_state([new, old], as_of=T0 + timedelta(days=2))
    assert [(s.signal_id, s.value) for s in state.signals] == [
        ("new-correction", "balanced now")
    ]
    assert "old-correction" in state.inactive_signal_ids


def test_conflicting_different_scope_state_is_preserved() -> None:
    durable = ev(
        "durable-interest",
        subject="okra",
        signal_type=SignalType.INTEREST,
        value="positive",
        scope=GLOBAL,
    )
    narrow = ev(
        "not-today",
        subject="okra",
        signal_type=SignalType.INTEREST,
        value="not today",
        scope=SESSION,
        lifetime=Lifetime.SESSION,
        at=T0 + timedelta(hours=1),
    )
    state = compile_recommendation_state([durable, narrow], as_of=T0 + timedelta(hours=2))
    assert {s.signal_id for s in state.signals} == {"durable-interest", "not-today"}


def test_no_implicit_time_decay_after_ninety_days() -> None:
    durable = ev(
        "old-interest",
        subject="fesenjan",
        signal_type=SignalType.INTEREST,
        value="high",
    )
    state = compile_recommendation_state([durable], as_of=T0 + timedelta(days=90))
    assert [s.signal_id for s in state.signals] == ["old-interest"]


def test_medium_term_signal_does_not_decay_without_explicit_transition() -> None:
    lane = ev(
        "consolidate",
        subject="subcontinental",
        subject_type=SubjectType.LANE,
        signal_type=SignalType.LANE,
        value="consolidate",
        scope=SignalScope(ScopeKind.LANE, "subcontinental"),
        lifetime=Lifetime.MEDIUM_TERM,
    )
    state = compile_recommendation_state([lane], as_of=T0 + timedelta(days=365))
    assert [(s.signal_id, s.lifetime) for s in state.signals] == [
        ("consolidate", Lifetime.MEDIUM_TERM)
    ]


def test_explicit_expiry_is_deterministic() -> None:
    expires = T0 + timedelta(days=7)
    signal = ev(
        "seasonal",
        subject="fresh peas",
        subject_type=SubjectType.PREREQUISITE,
        signal_type=SignalType.PREREQUISITE,
        value="season window",
        lifetime=Lifetime.UNTIL_EXPIRY,
        expires_at=expires,
        scope=SignalScope(ScopeKind.PREFERENCE, "season"),
    )
    before = compile_recommendation_state([signal], as_of=expires - timedelta(seconds=1))
    after = compile_recommendation_state([signal], as_of=expires)
    assert [s.signal_id for s in before.signals] == ["seasonal"]
    assert after.signals == ()


def test_soft_priority_tie_break_is_deterministic() -> None:
    state = compile_recommendation_state(
        [
            ev(
                "b",
                subject="x",
                subject_type=SubjectType.PREFERENCE,
                signal_type=SignalType.INTEREST,
                value="one",
                scope=GLOBAL,
            ),
            ev(
                "a",
                subject="y",
                subject_type=SubjectType.PREFERENCE,
                signal_type=SignalType.INTEREST,
                value="two",
                scope=GLOBAL,
            ),
        ],
        as_of=T0,
    )
    context = build_recommendation_context(
        state,
        candidate_keys=["candidate"],
        authoritative_eligibility=eligible("candidate"),
    )
    assert [s.signal_id for s in context.soft_signals] == ["a", "b"]


def test_clear_event_removes_target_without_mutating_history() -> None:
    target = ev(
        "parked",
        subject="candidate",
        signal_type=SignalType.SUPPRESSION,
        value="parked",
    )
    clear = RecommendationEvidence(
        event_id="clear-parked",
        kind=EventKind.CLEAR,
        subject_type=SubjectType.DISH,
        subject_key="candidate",
        signal_type=SignalType.SUPPRESSION,
        value="clear",
        scope=SignalScope(ScopeKind.DISH, "candidate"),
        strength=Strength.MEDIUM,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(days=1),
        source=EvidenceSource.EXPLICIT_MARCO,
        evidence_kind=EvidenceKind.EXPLICIT,
        provenance=("evidence:clear-parked",),
        clears=("parked",),
    )
    state = compile_recommendation_state([target, clear], as_of=T0 + timedelta(days=2))
    assert state.signals == ()
    assert {"parked", "clear-parked"}.issubset(state.inactive_signal_ids)


def test_recommendation_layer_accepts_scratchpad_authority_without_owning_lifecycle() -> None:
    parked = ev(
        "scratchpad-parked",
        subject="mapo tofu",
        signal_type=SignalType.SUPPRESSION,
        value="parked",
        source=EvidenceSource.SCRATCHPAD_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
    )
    state = compile_recommendation_state([parked], as_of=T0)
    signal = state.signals[0]
    assert signal.source is EvidenceSource.SCRATCHPAD_AUTHORITY
    assert signal.provenance == ("evidence:scratchpad-parked",)


def test_repeated_derived_suppression_folds_to_latest_without_becoming_hard() -> None:
    first = ev(
        "reject-1",
        subject="candidate",
        signal_type=SignalType.SUPPRESSION,
        value="declined",
        source=EvidenceSource.AGENT_INFERENCE,
        evidence_kind=EvidenceKind.DERIVED,
        strength=Strength.WEAK,
        confidence=Confidence.LOW,
    )
    second = ev(
        "reject-2",
        subject="candidate",
        signal_type=SignalType.SUPPRESSION,
        value="declined",
        at=T0 + timedelta(minutes=1),
        source=EvidenceSource.AGENT_INFERENCE,
        evidence_kind=EvidenceKind.DERIVED,
        strength=Strength.MEDIUM,
        confidence=Confidence.MEDIUM,
    )
    state = compile_recommendation_state([first, second], as_of=T0 + timedelta(minutes=2))
    assert len(state.signals) == 1
    signal = state.signals[0]
    assert signal.signal_id == "reject-2"
    assert signal.eligibility_effect is EligibilityEffect.SOFT
    assert signal.provenance == ("evidence:reject-1", "evidence:reject-2")


def test_weaker_later_inference_does_not_erase_explicit_same_scope_state() -> None:
    explicit = ev(
        "explicit",
        subject="spice",
        subject_type=SubjectType.PREFERENCE,
        signal_type=SignalType.PREFERENCE,
        value="likes spicy",
        scope=GLOBAL,
        evidence_kind=EvidenceKind.EXPLICIT,
    )
    inferred = ev(
        "inferred",
        subject="spice",
        subject_type=SubjectType.PREFERENCE,
        signal_type=SignalType.PREFERENCE,
        value="avoid spicy",
        scope=GLOBAL,
        at=T0 + timedelta(days=1),
        source=EvidenceSource.AGENT_INFERENCE,
        evidence_kind=EvidenceKind.DERIVED,
    )
    state = compile_recommendation_state([explicit, inferred], as_of=T0 + timedelta(days=2))
    assert {s.signal_id for s in state.signals} == {"explicit", "inferred"}


def test_weaker_same_value_inference_folds_into_stronger_provenance() -> None:
    explicit = ev(
        "explicit",
        subject="spice",
        subject_type=SubjectType.PREFERENCE,
        signal_type=SignalType.PREFERENCE,
        value="likes spicy",
        scope=GLOBAL,
        evidence_kind=EvidenceKind.EXPLICIT,
    )
    inferred = ev(
        "inferred",
        subject="spice",
        subject_type=SubjectType.PREFERENCE,
        signal_type=SignalType.PREFERENCE,
        value="likes spicy",
        scope=GLOBAL,
        at=T0 + timedelta(days=1),
        source=EvidenceSource.AGENT_INFERENCE,
        evidence_kind=EvidenceKind.DERIVED,
    )
    state = compile_recommendation_state([explicit, inferred], as_of=T0 + timedelta(days=2))
    assert [s.signal_id for s in state.signals] == ["explicit"]
    assert state.signals[0].provenance == ("evidence:explicit", "evidence:inferred")
    assert "inferred" in state.inactive_signal_ids


def test_hard_and_soft_prerequisite_state_do_not_implicitly_replace_each_other() -> None:
    hard = ev(
        "hard-prereq",
        subject="dish",
        signal_type=SignalType.PREREQUISITE,
        value="ingredient missing",
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        eligibility=EligibilityEffect.HARD_BLOCK,
    )
    soft = ev(
        "soft-prereq",
        subject="dish",
        signal_type=SignalType.PREREQUISITE,
        value="ingredient route uncertain",
        at=T0 + timedelta(minutes=1),
        source=EvidenceSource.EXPLICIT_MARCO,
        evidence_kind=EvidenceKind.EXPLICIT,
        eligibility=EligibilityEffect.SOFT,
    )
    state = compile_recommendation_state([hard, soft], as_of=T0 + timedelta(minutes=2))
    assert {s.signal_id for s in state.signals} == {"hard-prereq", "soft-prereq"}
    assert (
        build_recommendation_context(state, candidate_keys=["dish"])
        .eligibility_for("dish")
        .status
        is CandidateEligibility.BLOCKED
    )


def test_duplicate_evidence_identity_is_rejected() -> None:
    one = ev(
        "duplicate",
        subject="x",
        signal_type=SignalType.INTEREST,
        value="one",
    )
    two = ev(
        "duplicate",
        subject="y",
        signal_type=SignalType.INTEREST,
        value="two",
        at=T0 + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="event_id values must be unique"):
        compile_recommendation_state([one, two], as_of=T0 + timedelta(days=1))


def test_soft_limit_never_exceeds_versioned_cap() -> None:
    state = compile_recommendation_state([], as_of=T0)
    with pytest.raises(ValueError, match="soft_limit must be between"):
        build_recommendation_context(state, candidate_keys=[], soft_limit=MAX_SOFT_SIGNALS + 1)
