from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from operator_decision import (  # noqa: E402
    DecisionError,
    decision_identity,
    parse_decision_packet,
    record_decision_surface,
    resolve_marco_decision,
)

TASK = "1217504143564662"
BLOCKED = "1217429201656135"
READY = "1217419961932523"
NOW = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
QUESTION = "Choose A or B"
REV = "r3"
DID = decision_identity(task_gid=TASK, revision=REV, question=QUESTION)
NOTES = f"""<!-- dish-marco-decision:v1 id={DID} revision={REV} -->
Decision needed: {QUESTION}
Recommended answer: A
Alternatives / material tradeoff: B is slower but safer
Consequence of no decision: work remains blocked
What happens immediately after approval: move to Ready
"""


class FakeAsana:
    def __init__(self, *, name="MARCO DECISION — P1 — choose A or B", notes=NOTES, section_name=None):
        section = {"gid": BLOCKED}
        if section_name is not None:
            section["name"] = section_name
        self.task = {
            "gid": TASK,
            "name": name,
            "notes": notes,
            "memberships": [{"section": section}],
        }
        self.stories = []
        self.moves = 0

    def get_task(self, gid):
        return deepcopy(self.task)

    def get_stories(self, gid):
        return deepcopy(self.stories)

    def add_comment(self, gid, text):
        self.stories.append({"text": text})
        return {"gid": str(len(self.stories)), "text": text}

    def move_task_to_section(self, *, task_gid, section_gid):
        assert task_gid == TASK
        self.moves += 1
        self.task["memberships"] = [{"section": {"gid": section_gid}}]


def test_external_blocker_is_not_parsed_as_marco_decision():
    asana = FakeAsana(name="BLOCKED — P1 — waiting on PR #95")
    assert parse_decision_packet(asana.get_task(TASK)) is None
    assert record_decision_surface(asana=asana, task_gid=TASK, now=NOW) is None


def test_current_needs_human_review_task_can_carry_decision_packet_without_marco_decision_title():
    asana = FakeAsana(
        name="Review V4 — current task awaiting Marco decision",
        section_name="Needs Human Review",
    )
    assert parse_decision_packet(asana.get_task(TASK)).decision_id == DID
    assert record_decision_surface(asana=asana, task_gid=TASK, now=NOW) == "initial"


def test_decision_packet_requires_marker_at_start_and_all_fields():
    asana = FakeAsana(notes="preface\n" + NOTES)
    try:
        parse_decision_packet(asana.get_task(TASK))
    except DecisionError as exc:
        assert "must begin" in str(exc)
    else:
        raise AssertionError("expected malformed decision packet rejection")


def test_new_decision_surfaces_immediately_then_once_after_24h():
    asana = FakeAsana()
    assert record_decision_surface(asana=asana, task_gid=TASK, now=NOW) == "initial"
    assert record_decision_surface(asana=asana, task_gid=TASK, now=NOW + timedelta(hours=23)) is None
    assert record_decision_surface(asana=asana, task_gid=TASK, now=NOW + timedelta(hours=24)) == "reminder"
    assert record_decision_surface(asana=asana, task_gid=TASK, now=NOW + timedelta(days=3)) is None
    assert len(asana.stories) == 2


def test_changed_decision_revision_does_not_reuse_old_surface_state():
    asana = FakeAsana()
    assert record_decision_surface(asana=asana, task_gid=TASK, now=NOW) == "initial"
    new_question = "Choose A, B, or C"
    new_id = decision_identity(task_gid=TASK, revision="r4", question=new_question)
    asana.task["notes"] = NOTES.replace(f"id={DID} revision={REV}", f"id={new_id} revision=r4").replace(QUESTION, new_question)
    assert record_decision_surface(asana=asana, task_gid=TASK, now=NOW + timedelta(minutes=1)) == "initial"


def test_explicit_answer_binds_exact_revision_and_moves_out_of_blocked():
    asana = FakeAsana()
    resolve_marco_decision(
        asana=asana,
        task_gid=TASK,
        expected_decision_id=DID,
        answer="A",
        next_section_gid=READY,
        now=NOW,
    )
    assert asana.moves == 1
    assert asana.task["memberships"][0]["section"]["gid"] == READY
    assert "Answer: A" in asana.stories[-1]["text"]


def test_stale_answer_for_old_revision_fails_closed():
    asana = FakeAsana()
    try:
        resolve_marco_decision(
            asana=asana,
            task_gid=TASK,
            expected_decision_id="0" * 16,
            answer="A",
            next_section_gid=READY,
            now=NOW,
        )
    except DecisionError as exc:
        assert "current decision revision" in str(exc)
    else:
        raise AssertionError("expected stale decision answer rejection")
