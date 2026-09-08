from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dish_tool.recommendation_state import (
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
TARGET_SCOPE = SignalScope(ScopeKind.DISH, "sweet breakfast")


def _durable_exclusion() -> RecommendationEvidence:
    return RecommendationEvidence(
        event_id="explicit-exclude",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="sweet breakfast",
        signal_type=SignalType.EXCLUSION,
        value="do not recommend",
        scope=TARGET_SCOPE,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0,
        source=EvidenceSource.EXPLICIT_MARCO,
        evidence_kind=EvidenceKind.EXPLICIT,
        provenance=("evidence:explicit-exclude",),
        eligibility_effect=EligibilityEffect.HARD_EXCLUDE,
    )


def _clear(
    event_id: str,
    *,
    subject: str = "sweet breakfast",
    scope: SignalScope = TARGET_SCOPE,
    evidence_kind: EvidenceKind = EvidenceKind.AUTHORITATIVE,
) -> RecommendationEvidence:
    return RecommendationEvidence(
        event_id=event_id,
        kind=EventKind.CLEAR,
        subject_type=SubjectType.DISH,
        subject_key=subject,
        signal_type=SignalType.EXCLUSION,
        value="clear",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(minutes=1),
        source=(
            EvidenceSource.EXPLICIT_MARCO
            if evidence_kind is EvidenceKind.EXPLICIT
            else EvidenceSource.RUNTIME_FACT
        ),
        evidence_kind=evidence_kind,
        provenance=(f"evidence:{event_id}",),
        clears=("explicit-exclude",),
    )


def test_lower_authority_clear_cannot_retire_explicit_durable_exclusion() -> None:
    with pytest.raises(ValueError, match="cannot retire stronger evidence"):
        compile_recommendation_state(
            [_durable_exclusion(), _clear("weaker-clear")],
            as_of=T0 + timedelta(minutes=2),
        )


@pytest.mark.parametrize(
    "source",
    [
        EvidenceSource.AGENT_INFERENCE,
        EvidenceSource.IMPORTED_HISTORY,
        EvidenceSource.COOK_OUTCOME,
        EvidenceSource.RUNTIME_FACT,
    ],
)
def test_non_explicit_sources_cannot_claim_explicit_clear_authority(
    source: EvidenceSource,
) -> None:
    with pytest.raises(ValueError, match="explicit evidence requires an explicit source authority"):
        RecommendationEvidence(
            event_id=f"forged-clear-{source.value}",
            kind=EventKind.CLEAR,
            subject_type=SubjectType.DISH,
            subject_key="sweet breakfast",
            signal_type=SignalType.EXCLUSION,
            value="clear",
            scope=TARGET_SCOPE,
            strength=Strength.STRONG,
            confidence=Confidence.HIGH,
            lifetime=Lifetime.DURABLE,
            valid_from=T0 + timedelta(minutes=1),
            source=source,
            evidence_kind=EvidenceKind.EXPLICIT,
            provenance=(f"evidence:forged-clear-{source.value}",),
            clears=("explicit-exclude",),
        )


def test_agent_inference_cannot_claim_explicit_supersede_authority() -> None:
    with pytest.raises(ValueError, match="explicit evidence requires an explicit source authority"):
        RecommendationEvidence(
            event_id="forged-supersede",
            kind=EventKind.SET,
            subject_type=SubjectType.DISH,
            subject_key="sweet breakfast",
            signal_type=SignalType.SUPPRESSION,
            value="available again",
            scope=TARGET_SCOPE,
            strength=Strength.STRONG,
            confidence=Confidence.HIGH,
            lifetime=Lifetime.DURABLE,
            valid_from=T0 + timedelta(minutes=1),
            source=EvidenceSource.AGENT_INFERENCE,
            evidence_kind=EvidenceKind.EXPLICIT,
            provenance=("evidence:forged-supersede",),
            supersedes=("explicit-exclude",),
        )


def test_agent_inference_cannot_claim_authoritative_lifecycle_authority() -> None:
    with pytest.raises(ValueError, match="agent inference must use derived evidence"):
        RecommendationEvidence(
            event_id="forged-authoritative-clear",
            kind=EventKind.CLEAR,
            subject_type=SubjectType.DISH,
            subject_key="sweet breakfast",
            signal_type=SignalType.EXCLUSION,
            value="clear",
            scope=TARGET_SCOPE,
            strength=Strength.STRONG,
            confidence=Confidence.HIGH,
            lifetime=Lifetime.DURABLE,
            valid_from=T0 + timedelta(minutes=1),
            source=EvidenceSource.AGENT_INFERENCE,
            evidence_kind=EvidenceKind.AUTHORITATIVE,
            provenance=("evidence:forged-authoritative-clear",),
            clears=("authoritative-target",),
        )


@pytest.mark.parametrize(
    "source",
    [EvidenceSource.IMPORTED_HISTORY, EvidenceSource.COOK_OUTCOME],
)
def test_non_lifecycle_sources_cannot_retire_authoritative_state(
    source: EvidenceSource,
) -> None:
    with pytest.raises(ValueError, match="lifecycle relations require an authoritative source"):
        RecommendationEvidence(
            event_id=f"unauthorized-clear-{source.value}",
            kind=EventKind.CLEAR,
            subject_type=SubjectType.DISH,
            subject_key="sweet breakfast",
            signal_type=SignalType.EXCLUSION,
            value="clear",
            scope=TARGET_SCOPE,
            strength=Strength.STRONG,
            confidence=Confidence.HIGH,
            lifetime=Lifetime.DURABLE,
            valid_from=T0 + timedelta(minutes=1),
            source=source,
            evidence_kind=EvidenceKind.AUTHORITATIVE,
            provenance=(f"evidence:unauthorized-clear-{source.value}",),
            clears=("authoritative-target",),
        )


def test_unrelated_subject_clear_cannot_retire_explicit_durable_exclusion() -> None:
    with pytest.raises(ValueError, match="must match target subject and scope"):
        compile_recommendation_state(
            [
                _durable_exclusion(),
                _clear(
                    "unrelated-subject-clear",
                    subject="other dish",
                    evidence_kind=EvidenceKind.EXPLICIT,
                ),
            ],
            as_of=T0 + timedelta(minutes=2),
        )


def test_unrelated_scope_clear_cannot_retire_explicit_durable_exclusion() -> None:
    with pytest.raises(ValueError, match="must match target subject and scope"):
        compile_recommendation_state(
            [
                _durable_exclusion(),
                _clear(
                    "unrelated-scope-clear",
                    scope=SignalScope(ScopeKind.SESSION, "session-2"),
                    evidence_kind=EvidenceKind.EXPLICIT,
                ),
            ],
            as_of=T0 + timedelta(minutes=2),
        )


def test_lower_authority_supersede_cannot_make_excluded_candidate_actionable() -> None:
    replacement = RecommendationEvidence(
        event_id="weaker-supersede",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="sweet breakfast",
        signal_type=SignalType.SUPPRESSION,
        value="available again",
        scope=TARGET_SCOPE,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(minutes=1),
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("evidence:weaker-supersede",),
        supersedes=("explicit-exclude",),
    )
    with pytest.raises(ValueError, match="cannot retire stronger evidence"):
        compile_recommendation_state(
            [_durable_exclusion(), replacement],
            as_of=T0 + timedelta(minutes=2),
        )


def test_equal_authority_same_scope_clear_still_retires_exclusion() -> None:
    state = compile_recommendation_state(
        [
            _durable_exclusion(),
            _clear("explicit-clear", evidence_kind=EvidenceKind.EXPLICIT),
        ],
        as_of=T0 + timedelta(minutes=2),
    )
    result = build_recommendation_context(
        state,
        candidate_keys=["sweet breakfast"],
        authoritative_eligibility={
            "sweet breakfast": EligibilityObservation(
                CandidateEligibility.ELIGIBLE,
                ("eligibility:sweet breakfast",),
            )
        },
    ).eligibility_for("sweet breakfast")
    assert result.status is CandidateEligibility.ELIGIBLE


@pytest.mark.parametrize(
    "source",
    [EvidenceSource.SCRATCHPAD_AUTHORITY, EvidenceSource.PROFILE_AUTHORITY],
)
def test_cross_authority_clear_cannot_retire_runtime_hard_block(
    source: EvidenceSource,
) -> None:
    scope = SignalScope(ScopeKind.DISH, "rendang")
    blocker = RecommendationEvidence(
        event_id="runtime-block",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="pedigree unresolved",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0,
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("runtime:pedigree",),
        eligibility_effect=EligibilityEffect.HARD_BLOCK,
    )
    cross_authority_clear = RecommendationEvidence(
        event_id=f"cross-clear-{source.value}",
        kind=EventKind.CLEAR,
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="clear",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(minutes=1),
        source=source,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=(f"evidence:{source.value}",),
        clears=("runtime-block",),
    )
    with pytest.raises(ValueError, match="cross source authority"):
        compile_recommendation_state(
            [blocker, cross_authority_clear],
            as_of=T0 + timedelta(minutes=2),
        )


def test_cross_authority_supersede_cannot_retire_scratchpad_lifecycle() -> None:
    scope = SignalScope(ScopeKind.DISH, "zhong wontons")
    parked = RecommendationEvidence(
        event_id="scratchpad-parked",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="zhong wontons",
        signal_type=SignalType.SUPPRESSION,
        value="parked until chilli oil rebuild",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.UNTIL_WAKE,
        valid_from=T0,
        source=EvidenceSource.SCRATCHPAD_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("scratchpad:zhong-wontons",),
        wake_condition="chilli-oil-ready",
    )
    replacement = RecommendationEvidence(
        event_id="runtime-unpark",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="zhong wontons",
        signal_type=SignalType.SUPPRESSION,
        value="available",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(minutes=1),
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("runtime:chilli-oil-ready",),
        supersedes=("scratchpad-parked",),
    )
    with pytest.raises(ValueError, match="cross source authority"):
        compile_recommendation_state(
            [parked, replacement],
            as_of=T0 + timedelta(minutes=2),
        )


def test_cross_authority_clear_cannot_retire_profile_configuration() -> None:
    scope = SignalScope(ScopeKind.PREFERENCE, "spice")
    profile_preference = RecommendationEvidence(
        event_id="profile-spice",
        kind=EventKind.SET,
        subject_type=SubjectType.PREFERENCE,
        subject_key="spice",
        signal_type=SignalType.PREFERENCE,
        value="mild",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0,
        source=EvidenceSource.PROFILE_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("profile:spice",),
    )
    scratchpad_clear = RecommendationEvidence(
        event_id="scratchpad-clear-profile",
        kind=EventKind.CLEAR,
        subject_type=SubjectType.PREFERENCE,
        subject_key="spice",
        signal_type=SignalType.PREFERENCE,
        value="clear",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(minutes=1),
        source=EvidenceSource.SCRATCHPAD_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("scratchpad:clear-profile",),
        clears=("profile-spice",),
    )
    with pytest.raises(ValueError, match="cross source authority"):
        compile_recommendation_state(
            [profile_preference, scratchpad_clear],
            as_of=T0 + timedelta(minutes=2),
        )


def test_same_source_runtime_clear_can_retire_runtime_hard_block() -> None:
    scope = SignalScope(ScopeKind.DISH, "rendang")
    blocker = RecommendationEvidence(
        event_id="runtime-block",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="pedigree unresolved",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0,
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("runtime:pedigree",),
        eligibility_effect=EligibilityEffect.HARD_BLOCK,
    )
    clear = RecommendationEvidence(
        event_id="runtime-clear",
        kind=EventKind.CLEAR,
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="clear",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(minutes=1),
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("runtime:pedigree-resolved",),
        clears=("runtime-block",),
    )
    state = compile_recommendation_state(
        [blocker, clear],
        as_of=T0 + timedelta(minutes=2),
    )
    result = build_recommendation_context(
        state,
        candidate_keys=["rendang"],
        authoritative_eligibility={
            "rendang": EligibilityObservation(
                CandidateEligibility.ELIGIBLE,
                ("eligibility:rendang",),
            )
        },
    ).eligibility_for("rendang")
    assert result.status is CandidateEligibility.ELIGIBLE


@pytest.mark.parametrize(
    ("source", "evidence_kind"),
    [
        (EvidenceSource.EXPLICIT_MARCO, EvidenceKind.EXPLICIT),
        (EvidenceSource.SCRATCHPAD_AUTHORITY, EvidenceKind.AUTHORITATIVE),
        (EvidenceSource.PROFILE_AUTHORITY, EvidenceKind.AUTHORITATIVE),
    ],
)
def test_cross_authority_same_slot_set_cannot_displace_runtime_hard_block(
    source: EvidenceSource,
    evidence_kind: EvidenceKind,
) -> None:
    scope = SignalScope(ScopeKind.DISH, "rendang")
    blocker = RecommendationEvidence(
        event_id="runtime-slot-block",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="pedigree unresolved",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0,
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("runtime:slot-pedigree",),
        eligibility_effect=EligibilityEffect.HARD_BLOCK,
    )
    expiring_replacement = RecommendationEvidence(
        event_id=f"cross-slot-{source.value}",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="temporarily available",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.UNTIL_EXPIRY,
        valid_from=T0 + timedelta(minutes=1),
        source=source,
        evidence_kind=evidence_kind,
        provenance=(f"evidence:cross-slot-{source.value}",),
        eligibility_effect=EligibilityEffect.HARD_BLOCK,
        expires_at=T0 + timedelta(minutes=2),
    )
    state = compile_recommendation_state(
        [blocker, expiring_replacement],
        as_of=T0 + timedelta(minutes=3),
    )
    result = build_recommendation_context(
        state,
        candidate_keys=["rendang"],
        authoritative_eligibility={
            "rendang": EligibilityObservation(
                CandidateEligibility.ELIGIBLE,
                ("eligibility:rendang",),
            )
        },
    ).eligibility_for("rendang")
    assert result.status is CandidateEligibility.BLOCKED
    assert {signal.signal_id for signal in state.signals} == {"runtime-slot-block"}


def test_cross_authority_same_slot_set_preserves_profile_configuration() -> None:
    scope = SignalScope(ScopeKind.PREFERENCE, "spice")
    profile_preference = RecommendationEvidence(
        event_id="profile-slot-spice",
        kind=EventKind.SET,
        subject_type=SubjectType.PREFERENCE,
        subject_key="spice",
        signal_type=SignalType.PREFERENCE,
        value="mild",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0,
        source=EvidenceSource.PROFILE_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("profile:slot-spice",),
    )
    scratchpad_conflict = RecommendationEvidence(
        event_id="scratchpad-slot-spice",
        kind=EventKind.SET,
        subject_type=SubjectType.PREFERENCE,
        subject_key="spice",
        signal_type=SignalType.PREFERENCE,
        value="hot",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(minutes=1),
        source=EvidenceSource.SCRATCHPAD_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("scratchpad:slot-spice",),
    )
    state = compile_recommendation_state(
        [profile_preference, scratchpad_conflict],
        as_of=T0 + timedelta(minutes=2),
    )
    assert {signal.signal_id for signal in state.signals} == {
        "profile-slot-spice",
        "scratchpad-slot-spice",
    }


def test_cross_authority_same_slot_set_preserves_scratchpad_lifecycle() -> None:
    scope = SignalScope(ScopeKind.DISH, "zhong wontons")
    parked = RecommendationEvidence(
        event_id="scratchpad-slot-parked",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="zhong wontons",
        signal_type=SignalType.SUPPRESSION,
        value="parked until chilli oil rebuild",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.UNTIL_WAKE,
        valid_from=T0,
        source=EvidenceSource.SCRATCHPAD_AUTHORITY,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("scratchpad:slot-zhong-wontons",),
        wake_condition="chilli-oil-ready",
    )
    runtime_conflict = RecommendationEvidence(
        event_id="runtime-slot-available",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="zhong wontons",
        signal_type=SignalType.SUPPRESSION,
        value="available",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(minutes=1),
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("runtime:slot-available",),
    )
    state = compile_recommendation_state(
        [parked, runtime_conflict],
        as_of=T0 + timedelta(minutes=2),
    )
    assert {signal.signal_id for signal in state.signals} == {
        "runtime-slot-available",
        "scratchpad-slot-parked",
    }


def test_same_source_later_set_still_replaces_same_slot_state() -> None:
    scope = SignalScope(ScopeKind.DISH, "rendang")
    old_blocker = RecommendationEvidence(
        event_id="runtime-old-block",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="pedigree unresolved",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0,
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("runtime:old-block",),
        eligibility_effect=EligibilityEffect.HARD_BLOCK,
        fact_key="rendang-availability",
    )
    new_blocker = RecommendationEvidence(
        event_id="runtime-new-block",
        kind=EventKind.SET,
        subject_type=SubjectType.DISH,
        subject_key="rendang",
        signal_type=SignalType.BLOCKER,
        value="sourcing route unavailable",
        scope=scope,
        strength=Strength.STRONG,
        confidence=Confidence.HIGH,
        lifetime=Lifetime.DURABLE,
        valid_from=T0 + timedelta(minutes=1),
        source=EvidenceSource.RUNTIME_FACT,
        evidence_kind=EvidenceKind.AUTHORITATIVE,
        provenance=("runtime:new-block",),
        eligibility_effect=EligibilityEffect.HARD_BLOCK,
        fact_key="rendang-availability",
    )
    state = compile_recommendation_state(
        [old_blocker, new_blocker],
        as_of=T0 + timedelta(minutes=2),
    )
    assert [signal.signal_id for signal in state.signals] == ["runtime-new-block"]
    assert "runtime-old-block" in state.inactive_signal_ids
