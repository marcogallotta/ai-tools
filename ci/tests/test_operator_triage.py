from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from operator_triage import TriageBucket, TriageInput, classify  # noqa: E402

TASK = "1217512308376614"


def test_implementation_ready_is_send_now_only_after_reconciliation():
    result = classify(TriageInput(TASK, implementation_ready=True))
    assert result.bucket is TriageBucket.SEND_NOW
    assert "final live sanity check" in result.next_action


def test_research_needed_never_becomes_implementation_send_now():
    result = classify(
        TriageInput(
            TASK,
            implementation_ready=True,
            research_needed=True,
            research_dispatchable=True,
        )
    )
    assert result.bucket is TriageBucket.NEEDS_RESEARCH
    assert "research/design" in result.next_action


def test_critical_research_surfaces_but_lower_priority_research_does_not_interrupt():
    critical = classify(TriageInput(TASK, research_needed=True, critical_research=True))
    routine = classify(TriageInput(TASK, research_needed=True, critical_research=False))
    assert critical.bucket is TriageBucket.NEEDS_RESEARCH
    assert critical.surface_to_marco is True
    assert routine.surface_to_marco is False


def test_blocked_or_live_contradiction_stays_out_of_send_now():
    blocked = classify(TriageInput(TASK, implementation_ready=True, blocked_reason="PR #95"))
    assert blocked.bucket is TriageBucket.BLOCKED_WAITING
    stale = classify(
        TriageInput(TASK, implementation_ready=True, live_contradiction="task already implemented")
    )
    assert stale.bucket is TriageBucket.BLOCKED_WAITING
    assert stale.reconciliation_required is True


def test_unknown_readiness_fails_closed_for_reconciliation():
    result = classify(TriageInput(TASK))
    assert result.bucket is TriageBucket.BLOCKED_WAITING
    assert result.reconciliation_required is True
