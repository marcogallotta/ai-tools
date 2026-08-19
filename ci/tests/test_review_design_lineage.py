import hashlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from review_design_lineage import (  # noqa: E402
    Challenge,
    ChallengeKey,
    Disposition,
    Event,
    EventType,
    Identity,
    Projection,
    ReviewDisposition,
    State,
    challenge_used,
    consume_identity,
    create_generation,
    cumulative_drift_baseline,
    external_snapshot_contradictions,
    projection_contradictions,
    reconstruct,
    recover_notes,
    recover_snapshot,
    require_challenge_available,
    validate_disposition,
    validate_lineage,
)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def gen(gid, text, predecessor=None):
    return create_generation(
        task_gid="1217613446414340",
        generation_id=gid,
        snapshot=text.encode(),
        predecessor_generation_id=predecessor,
        relevant_repo_baseline="7dd4259efc071473c91a34498692ed8951f5d7fb",
        created_at="2026-08-19T00:00:00Z",
        created_by="Dish Agent: Review",
    )


def event(record, gid, kind, successor=None):
    return Event(
        gid,
        kind,
        record.identity,
        "2026-08-19T00:01:00Z",
        "Dish Agent",
        successor,
    )


def test_asana_design_recovery_uses_review_v2_snapshot():
    g5 = gen("G5", "approved design")
    result = recover_notes(
        design_bearing=True,
        generation=g5,
        generic_preimage=b"wrong",
    )
    assert result.snapshot == b"approved design"
    assert result.identity == g5.identity
    assert result.source == "dish-design-generation:v1"


def test_parallel_snapshot_conflict_surfaces_and_review_v2_wins():
    g5 = gen("G5", "authoritative")
    problems = external_snapshot_contradictions(
        g5,
        b"different",
        "lifecycle-cache",
    )
    assert problems[0].code == "competing-design-snapshot"
    assert recover_snapshot(g5) == b"authoritative"


def test_non_design_preimage_stays_outside_design_lineage():
    result = recover_notes(
        design_bearing=False,
        generic_preimage=b"operational",
    )
    assert (result.source, result.identity, result.snapshot) == (
        "generic-notes-preimage",
        None,
        b"operational",
    )


def test_lifecycle_and_service_consume_exact_identity_without_minting():
    g5 = gen("G5", "dispatch")
    identity = consume_identity(g5)
    assert identity == g5.identity
    assert Identity(*identity.tuple()) == identity


def test_successor_design_stays_in_review_v2_lineage_and_projection_moves():
    g5 = gen("G5", "dispatched")
    h5 = [
        event(g5, "1", EventType.CREATED),
        event(g5, "2", EventType.MARCO_APPROVED),
        event(g5, "3", EventType.DISPATCHED),
        event(g5, "4", EventType.REOPENED),
        event(g5, "5", EventType.SUPERSEDED, "G6"),
    ]
    g6 = gen("G6", "changed", "G5")
    h6 = [event(g6, "6", EventType.CREATED)]
    states = {
        "G5": reconstruct(g5, h5),
        "G6": reconstruct(g6, h6),
    }
    assert validate_lineage([g5, g6], states) == ()
    pointer = Projection(g6.identity, "record-g6", State.AUTHORING, "6")
    assert projection_contradictions(
        g6,
        states["G6"],
        pointer,
        "record-g6",
    ) == ()


def test_replacement_author_and_reviewer_do_not_reset_challenge_budget():
    g5 = gen("G5", "candidate")
    key = ChallengeKey(g5.identity, "blocker", sha("evidence-set"))
    challenge = Challenge(key, "replacement-author", sha("challenge"))
    assert challenge_used(key, [challenge])
    with pytest.raises(ValueError, match="challenge budget"):
        require_challenge_available(key, [challenge])
    validate_disposition(
        challenge,
        ReviewDisposition(key, "replacement-reviewer", Disposition.NARROWS),
        ["original-author", "replacement-author"],
    )


def test_material_author_cannot_self_clear_candidate():
    g5 = gen("G5", "candidate")
    key = ChallengeKey(g5.identity, "blocker", sha("evidence-set"))
    challenge = Challenge(key, "author-b", sha("challenge"))
    with pytest.raises(ValueError, match="cannot independently clear"):
        validate_disposition(
            challenge,
            ReviewDisposition(key, "author-b", Disposition.WITHDRAWS),
            ["author-a", "author-b"],
        )


def test_invalid_events_and_pointer_disagreement_are_contradictions():
    g5 = gen("G5", "candidate")
    state = reconstruct(
        g5,
        [
            event(g5, "1", EventType.CREATED),
            event(g5, "2", EventType.DISPATCHED),
        ],
    )
    assert state.state is State.AUTHORING
    assert any(c.code == "invalid-transition" for c in state.contradictions)
    other = gen("G6", "other", "G5")
    pointer = Projection(other.identity, "wrong", State.DISPATCHED, "wrong")
    assert {
        c.code
        for c in projection_contradictions(g5, state, pointer, "record-g5")
    } == {
        "projection-identity-mismatch",
        "projection-record-mismatch",
        "projection-state-mismatch",
        "projection-event-mismatch",
    }


def test_dispatched_successor_without_reopen_is_surfaced():
    g5 = gen("G5", "dispatched")
    state = reconstruct(
        g5,
        [
            event(g5, "1", EventType.CREATED),
            event(g5, "2", EventType.MARCO_APPROVED),
            event(g5, "3", EventType.DISPATCHED),
        ],
    )
    g6 = gen("G6", "changed", "G5")
    problems = validate_lineage([g5, g6], {"G5": state})
    assert any(
        c.code == "dispatched-successor-without-reopen"
        for c in problems
    )


def test_cumulative_drift_uses_last_exact_marco_approved_ancestor():
    g4 = gen("G4", "approved")
    g5 = gen("G5", "delta1", "G4")
    g6 = gen("G6", "delta2", "G5")
    histories = {
        "G4": [
            event(g4, "1", EventType.CREATED),
            event(g4, "2", EventType.MARCO_APPROVED),
        ],
        "G5": [event(g5, "3", EventType.CREATED)],
        "G6": [event(g6, "4", EventType.CREATED)],
    }
    assert cumulative_drift_baseline(
        "G6",
        [g4, g5, g6],
        histories,
    ) == g4
