from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_projection import build_projection
from pr_lifecycle_support import LifecycleState, PRLifecycle, STATE_LABELS


HEAD = "a" * 40
OTHER_HEAD = "b" * 40
TASK = "1217611794618560"
REPOSITORY = "marcogallotta/ai-tools"


def lifecycle(
    *,
    state: LifecycleState = LifecycleState.INTEGRATION_READY,
    reviewed_head: str | None = HEAD,
    review_verdict: str | None = "MERGE",
    human_action: str | None = None,
) -> PRLifecycle:
    return PRLifecycle(
        number=179,
        url="https://github.com/marcogallotta/ai-tools/pull/179",
        title="Lifecycle V3 shadow test",
        head=HEAD,
        branch="agent/lifecycle-v3",
        base="main",
        draft=False,
        state=state,
        state_label=STATE_LABELS[state],
        task_ids=[TASK],
        review_verdict=review_verdict,
        reviewed_head=reviewed_head,
        gate={"diagnosis": "READY"},
        human_action=human_action,
    )


def source(state: str = "NOT_LANDED") -> dict[str, object]:
    return {
        "status": "COMPLETE",
        "pull_requests": {
            "179": {
                "state": state,
                "ultimate_target": "main",
                "publication_state": "open",
                "provenance": "fixture",
            }
        },
        "workstreams": [],
    }


def projection(value: PRLifecycle, *, completed: bool = False, source_state: str = "NOT_LANDED") -> dict[str, object]:
    return build_projection(
        [value],
        repository=REPOSITORY,
        tasks=[{"gid": TASK, "completed": completed}],
        source_observation=source(source_state),
    )


def shadow_decision(value: PRLifecycle, **kwargs: object) -> dict[str, object]:
    payload = projection(value, **kwargs)
    return payload["v3_shadow"]["decisions"][0]


def test_v3_shadow_admits_existing_v1a_boundary_without_write_authority():
    payload = projection(lifecycle())
    shadow = payload["v3_shadow"]
    decision = shadow["decisions"][0]

    assert shadow["schema"] == "dish-pr-lifecycle-v3-shadow-v1"
    assert shadow["mode"] == "SHADOW"
    assert shadow["activation_authorized"] is False
    assert shadow["write_authority"] is False
    assert shadow["authoritative_landing_path"] == "integration-v1a-local-fenced"
    assert shadow["human_hold_evaluation"] == "NOT_IMPLEMENTED_STAGE_1"
    assert decision["decision"] == "WOULD_ADMIT_EXISTING_INTEGRATION"
    assert decision["decision_scope"] == "current-v1a-mechanical-admission"
    assert decision["mutation_permitted"] is False
    assert decision["write_authority"] is False


def test_v3_shadow_observes_existing_integration_writer_instead_of_contending():
    decision = shadow_decision(lifecycle(state=LifecycleState.MERGING))

    assert decision["decision"] == "OBSERVE_EXISTING_WRITER"
    assert decision["mutation_permitted"] is False


def test_v3_shadow_blocks_reviewed_head_mismatch_at_existing_boundary():
    decision = shadow_decision(lifecycle(reviewed_head=OTHER_HEAD))

    assert decision["decision"] == "BLOCK_EXACT_HEAD_REVIEW"
    assert decision["current_reviewed_head"] == OTHER_HEAD
    assert decision["head"] == HEAD


def test_v3_shadow_does_not_turn_generic_operator_action_into_merge_veto():
    action = "give PR #179 to a local Implementation agent"
    decision = shadow_decision(lifecycle(human_action=action))

    assert decision["decision"] == "WOULD_ADMIT_EXISTING_INTEGRATION"
    assert decision["current_operator_action"] == action
    assert decision["mutation_permitted"] is False


def test_v3_shadow_does_not_turn_projection_contradiction_into_merge_veto():
    payload = projection(lifecycle(), completed=True)
    decision = payload["v3_shadow"]["decisions"][0]

    assert payload["resolved_lifecycle"][0]["truth"] == "CONTRADICTION"
    assert decision["truth"] == "CONTRADICTION"
    assert decision["decision"] == "WOULD_ADMIT_EXISTING_INTEGRATION"
    assert decision["mutation_permitted"] is False


def test_v3_shadow_requires_not_landed_source_identity():
    decision = shadow_decision(lifecycle(), source_state="LANDED")

    assert decision["decision"] == "BLOCK_SOURCE_IDENTITY"
    assert decision["mutation_permitted"] is False
