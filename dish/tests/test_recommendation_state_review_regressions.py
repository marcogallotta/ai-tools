from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dish_tool.recommendation_state import (
    MAX_ELIGIBILITY_REASONS,
    MAX_PROVENANCE_POINTERS,
    CandidateEligibility,
    Confidence,
    EligibilityEffect,
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


def _event(
    event_id: str,
    *,
    subject_type: SubjectType,
    subject_key: str,
    signal_type: SignalType,
    value: str,
    scope: SignalScope,
    source: EvidenceSource,
    evidence_kind: EvidenceKind,
    at: datetime,
    lifetime: Lifetime = Lifetime.DURABLE,
    eligibility_effect: EligibilityEffect = EligibilityEffect.SOFT,
    supersedes: tuple[str, ...] = (),
) -> RecommendationEvidence:
    return RecommendationEvidence(
        event_id=event_id,
        kind=EventKind.SET,
        subject_type=subject_type,
        subject_key=subject_key,
        signal_type=signal_type,
        value=value,
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=lifetime,
        valid_from=at,
        source=source,
        evidence_kind=evidence_kind,
        provenance=(f"evidence:{event_id}",),
        eligibility_effect=eligibility_effect,
        supersedes=supersedes,
    )


def test_repeated_same_slot_history_keeps_projection_provenance_bounded() -> None:
    scope = SignalScope(ScopeKind.DISH, "mapo tofu")
    events = [
        _event(
            f"repeat-{index:02d}",
            subject_type=SubjectType.DISH,
            subject_key="mapo tofu",
            signal_type=SignalType.SUPPRESSION,
            value="not this turn",
            scope=scope,
            source=EvidenceSource.AGENT_INFERENCE,
            evidence_kind=EvidenceKind.DERIVED,
            at=T0 + timedelta(minutes=index),
        )
        for index in range(MAX_PROVENANCE_POINTERS * 3)
    ]

    state = compile_recommendation_state(events, as_of=T0 + timedelta(days=1))

    assert len(state.signals) == 1
    assert state.signals[0].signal_id == f"repeat-{MAX_PROVENANCE_POINTERS * 3 - 1:02d}"
    assert len(state.signals[0].provenance) == MAX_PROVENANCE_POINTERS
    assert state.signals[0].provenance == tuple(
        f"evidence:repeat-{index:02d}"
        for index in range(MAX_PROVENANCE_POINTERS * 2, MAX_PROVENANCE_POINTERS * 3)
    )


def test_candidate_hard_eligibility_result_keeps_reason_and_provenance_fan_in_bounded() -> None:
    events = [
        _event(
            f"block-{index:02d}",
            subject_type=SubjectType.DISH,
            subject_key="rendang",
            signal_type=SignalType.PREREQUISITE,
            value=f"missing prerequisite {index}",
            scope=SignalScope(ScopeKind.CANDIDATE, f"rendang-{index:02d}"),
            source=EvidenceSource.RUNTIME_FACT,
            evidence_kind=EvidenceKind.AUTHORITATIVE,
            at=T0 + timedelta(minutes=index),
            eligibility_effect=EligibilityEffect.HARD_BLOCK,
        )
        for index in range(MAX_PROVENANCE_POINTERS * 3)
    ]
    state = compile_recommendation_state(events, as_of=T0 + timedelta(days=1))

    result = build_recommendation_context(
        state,
        candidate_keys=["rendang"],
    ).eligibility_for("rendang")

    assert result.status is CandidateEligibility.BLOCKED
    assert len(result.provenance) == MAX_PROVENANCE_POINTERS
    assert len(result.reasons) == MAX_ELIGIBILITY_REASONS


def test_profile_current_configuration_retires_older_direct_recommendation_evidence() -> None:
    scope = SignalScope(ScopeKind.PREFERENCE, "spice")
    old_direct = _event(
        "old-direct-spice",
        subject_type=SubjectType.PREFERENCE,
        subject_key="spice",
        signal_type=SignalType.PREFERENCE,
        value="hot",
        scope=scope,
        source=EvidenceSource.EXPLICIT_MARCO,
        evidence_kind=EvidenceKind.EXPLICIT,
        at=T0,
    )
    current_profile = _event(
        "profile-spice",
        subject_type=SubjectType.PREFERENCE,
        subject_key="spice",
        signal_type=SignalType.PREFERENCE,
        value="mild",
        scope=scope,
        source=EvidenceSource.PROFILE_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        at=T0 + timedelta(minutes=1),
    )

    state = compile_recommendation_state(
        [old_direct, current_profile],
        as_of=T0 + timedelta(minutes=2),
    )
    context = build_recommendation_context(state, candidate_keys=["laksa"])

    assert [signal.signal_id for signal in state.signals] == ["profile-spice"]
    assert "old-direct-spice" in state.inactive_signal_ids
    assert [signal.signal_id for signal in context.soft_signals] == ["profile-spice"]


def test_profile_can_explicitly_supersede_older_direct_preference() -> None:
    scope = SignalScope(ScopeKind.PREFERENCE, "spice")
    old_direct = _event(
        "old-direct-spice",
        subject_type=SubjectType.PREFERENCE,
        subject_key="spice",
        signal_type=SignalType.PREFERENCE,
        value="hot",
        scope=scope,
        source=EvidenceSource.EXPLICIT_MARCO,
        evidence_kind=EvidenceKind.EXPLICIT,
        at=T0,
    )
    current_profile = _event(
        "profile-spice",
        subject_type=SubjectType.PREFERENCE,
        subject_key="spice",
        signal_type=SignalType.PREFERENCE,
        value="mild",
        scope=scope,
        source=EvidenceSource.PROFILE_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        at=T0 + timedelta(minutes=1),
        supersedes=("old-direct-spice",),
    )

    state = compile_recommendation_state(
        [old_direct, current_profile],
        as_of=T0 + timedelta(minutes=2),
    )

    assert [signal.signal_id for signal in state.signals] == ["profile-spice"]


@pytest.mark.parametrize(
    ("source", "evidence_kind"),
    [
        (EvidenceSource.AGENT_INFERENCE, EvidenceKind.DERIVED),
        (EvidenceSource.IMPORTED_HISTORY, EvidenceKind.AUTHORITATIVE),
        (EvidenceSource.COOK_OUTCOME, EvidenceKind.AUTHORITATIVE),
    ],
)
def test_later_direct_evidence_retires_lower_authority_same_slot_state(
    source: EvidenceSource,
    evidence_kind: EvidenceKind,
) -> None:
    scope = SignalScope(ScopeKind.DISH, "fesenjan")
    old = _event(
        "old-evidence",
        subject_type=SubjectType.DISH,
        subject_key="fesenjan",
        signal_type=SignalType.INTEREST,
        value="uncertain",
        scope=scope,
        source=source,
        evidence_kind=evidence_kind,
        at=T0,
    )
    direct = _event(
        "direct-evidence",
        subject_type=SubjectType.DISH,
        subject_key="fesenjan",
        signal_type=SignalType.INTEREST,
        value="strong interest",
        scope=scope,
        source=EvidenceSource.EXPLICIT_MARCO,
        evidence_kind=EvidenceKind.EXPLICIT,
        at=T0 + timedelta(minutes=1),
    )

    state = compile_recommendation_state([old, direct], as_of=T0 + timedelta(minutes=2))

    assert [signal.signal_id for signal in state.signals] == ["direct-evidence"]
    assert "old-evidence" in state.inactive_signal_ids


def test_profile_still_cannot_retire_runtime_owned_hard_block() -> None:
    scope = SignalScope(ScopeKind.DISH, "rendang")
    runtime_block = _event(
        "runtime-block",
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="pedigree unresolved",
        scope=scope,
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        at=T0,
        eligibility_effect=EligibilityEffect.HARD_BLOCK,
    )
    profile_attempt = _event(
        "profile-attempt",
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="available",
        scope=scope,
        source=EvidenceSource.PROFILE_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        at=T0 + timedelta(minutes=1),
        eligibility_effect=EligibilityEffect.HARD_BLOCK,
        supersedes=("runtime-block",),
    )

    with pytest.raises(ValueError, match="cross source authority"):
        compile_recommendation_state(
            [runtime_block, profile_attempt],
            as_of=T0 + timedelta(minutes=2),
        )
