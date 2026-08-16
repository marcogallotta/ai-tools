from pathlib import Path

POLICY = Path(__file__).resolve().parents[2] / "OPERATOR_CONTROL_PLANE.md"


def test_true_ready_requires_actual_dispatchability():
    text = POLICY.read_text()
    for predicate in (
        "no unresolved Asana dependency",
        "no pending Marco-only decision",
        "no required prior design/readiness review",
        "no known active competing implementation lineage",
    ):
        assert predicate in text
    assert "missing/stale metadata never authorizes dispatch" in text
    assert "one live sanity check" in text


def test_blockers_and_code_area_are_visible_without_becoming_authority():
    text = POLICY.read_text()
    assert "[blocked on <gid>]" in text
    assert "CODE AREA:" in text
    assert "first-pass overlap hint only" in text
    assert "cannot authorize work by itself" in text


def test_true_ready_does_not_create_parallel_scheduler_or_queue():
    text = POLICY.read_text()
    assert "no scheduler, second queue, or ownership service" in text
    assert "Do not create a specialist scheduler" in text
