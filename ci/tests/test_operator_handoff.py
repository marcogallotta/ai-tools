from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from operator_handoff import (  # noqa: E402
    HandoffError,
    record_implementation_handoff,
    record_stale_handoff_alert,
)

TASK = "1217467755396235"
READY = "1217419961932523"
IN_PROGRESS = "1217419992928161"
NOW = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)


class FakeAsana:
    def __init__(self, *, readback_move=True, readback_comment=True):
        self.task = {"gid": TASK, "memberships": [{"section": {"gid": READY}}]}
        self.stories = []
        self.readback_move = readback_move
        self.readback_comment = readback_comment
        self.moves = 0
        self.comments = 0

    def get_task(self, gid):
        assert gid == TASK
        return deepcopy(self.task)

    def get_stories(self, gid):
        assert gid == TASK
        return deepcopy(self.stories)

    def add_comment(self, gid, text):
        self.comments += 1
        if self.readback_comment:
            self.stories.append({"text": text})
        return {"gid": f"story-{self.comments}", "text": text}

    def move_task_to_section(self, *, task_gid, section_gid):
        assert task_gid == TASK
        self.moves += 1
        if self.readback_move:
            self.task["memberships"] = [{"section": {"gid": section_gid}}]


def record(asana):
    return record_implementation_handoff(
        asana=asana,
        task_gid=TASK,
        ready_section_gid=READY,
        in_progress_section_gid=IN_PROGRESS,
        target_role="Implementation",
        timestamp=NOW,
        source="Coordinator",
        branch="agent/work",
        base="a" * 40,
    )


def test_authorized_handoff_moves_ready_to_in_progress_and_reads_back():
    asana = FakeAsana()
    result = record(asana)
    assert result.task_gid == TASK
    assert asana.task["memberships"][0]["section"]["gid"] == IN_PROGRESS
    assert asana.moves == 1
    assert asana.comments == 1
    assert "PR: not yet known" in asana.stories[0]["text"]


def test_handoff_does_not_claim_durable_when_move_or_record_readback_fails():
    for kwargs in ({"readback_move": False}, {"readback_comment": False}):
        asana = FakeAsana(**kwargs)
        try:
            record(asana)
        except HandoffError:
            pass
        else:
            raise AssertionError("expected fail-closed handoff readback")


def test_retry_is_idempotent_on_same_handoff_identity():
    asana = FakeAsana()
    first = record(asana)
    second = record(asana)
    assert first.handoff_id == second.handoff_id
    assert asana.comments == 1
    assert asana.moves == 1


def test_three_hour_no_evidence_alert_is_once_and_never_redispatches():
    asana = FakeAsana()
    handoff = record(asana)
    assert record_stale_handoff_alert(
        asana=asana,
        record=handoff,
        now=NOW + timedelta(hours=2, minutes=59),
        authoritative_implementation_evidence=False,
    ) is False
    assert record_stale_handoff_alert(
        asana=asana,
        record=handoff,
        now=NOW + timedelta(hours=3),
        authoritative_implementation_evidence=False,
    ) is True
    assert record_stale_handoff_alert(
        asana=asana,
        record=handoff,
        now=NOW + timedelta(hours=5),
        authoritative_implementation_evidence=False,
    ) is False
    assert "do not duplicate, replace, or redispatch" in asana.stories[-1]["text"]
    assert asana.moves == 1


def test_authoritative_implementation_evidence_suppresses_stale_alert():
    asana = FakeAsana()
    handoff = record(asana)
    assert record_stale_handoff_alert(
        asana=asana,
        record=handoff,
        now=NOW + timedelta(hours=4),
        authoritative_implementation_evidence=True,
    ) is False
