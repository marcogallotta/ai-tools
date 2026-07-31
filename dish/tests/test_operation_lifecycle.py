import pytest

from dish_tool.errors import DishRuleError
from dish_tool.lifecycle import assert_transition, hold, resumed

BASE = {
    "Status": "pending-verification",
    "Status detail": "None",
    "Resume status": "None",
    "Verification protocol release": "sha256:test",
    "Researched by": "ChatGPT — GPT-5, 2026-07-25",
    "Verified by": "None",
    "Self-verified": "ChatGPT — GPT-5, 2026-07-25",
}


def test_legal_protocol_transitions_are_explicit():
    expected = [
        ("research_handoff", "pending-research", "pending-verification"),
        ("approve", "pending-verification", "ready"),
        ("material_edit", "ready", "pending-verification"),
        ("non_material_edit", "ready", "ready"),
        ("submit", "ready", "ready"),
    ]
    transitions = [
        assert_transition(action=action, before=source, after=target)
        for action, source, target in expected
    ]
    assert [
        (transition.action, transition.source, transition.target)
        for transition in transitions
    ] == expected


def test_illegal_jump_fails_closed():
    with pytest.raises(DishRuleError) as exc:
        assert_transition(action="approve", before="pending-research", after="ready")
    assert exc.value.rule == "illegal_task_transition"


def test_hold_resumes_only_to_recorded_phase():
    held = hold(BASE, target="pending-evidence", detail="Need measured salinity", resume_status="pending-verification")
    restored = resumed(held.values)
    assert restored.values["Status"] == "pending-verification"
    assert restored.values["Status detail"] == "None"
    assert restored.values["Resume status"] == "None"


def test_invalid_resume_target_fails_closed():
    values = dict(BASE, Status="pending-human-review", **{"Status detail": "Decision needed", "Resume status": "ready"})
    with pytest.raises(DishRuleError) as exc:
        resumed(values)
    assert exc.value.rule == "illegal_task_transition"
