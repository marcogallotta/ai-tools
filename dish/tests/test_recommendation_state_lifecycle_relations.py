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
