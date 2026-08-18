from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pr_lifecycle_workstream as workstream
from pr_lifecycle_helpers import _integration_order_reason


NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
WORKSTREAM = "1217513381744783"
HEADS = {
    151: "6731669d731af424578001c66d8b61691301d96d",
    157: "c7a907ec9f09d4ddddcc6fb74222283bc2069fcf",
    159: "6962119bf1fe490ef5b30366a24dd7af9e8d5630",
    160: "7af143a9366905b46a2859cb50d59b0344f0f7d0",
}
ORDERS = {151: None, 157: "#151", 159: "#157", 160: "#159"}


def _member(number: int, slot: int, *, head: str | None = None, order: str | None = None):
    body = "" if order is None else f"INTEGRATION BLOCKED BY: {order}"
    return workstream.WorkstreamMember(
        slot=slot,
        total=4,
        pr_number=number,
        pr_url=f"https://github.com/marcogallotta/ai-tools/pull/{number}",
        branch=f"agent/stage-{slot}",
        base="main" if slot == 1 else f"agent/stage-{slot - 1}",
        head=head or HEADS[number],
        publication_state="open",
        task_ids=(str(1217500000000000 + slot), WORKSTREAM),
        owning_task=str(1217500000000000 + slot),
        integration_order=_integration_order_reason(None, {"body": body}),
    )


def _candidate(*, order_159: str = "#157", head_157: str | None = None):
    return workstream.build_candidate(
        WORKSTREAM,
        (
            _member(151, 1, order=None),
            _member(157, 2, order="#151", head=head_157),
            _member(159, 3, order=order_159),
            _member(160, 4, order="#159"),
        ),
    )


class GitHub:
    def __init__(self):
        self.reviews = {number: [] for number in HEADS}

    def get_reviews(self, number):
        return deepcopy(self.reviews[number])


def _review(github: GitHub, candidate, verdict="MERGE", *, divergent_order_pr=None):
    for member in candidate.members:
        extra = ""
        if member.pr_number == divergent_order_pr:
            extra = "\nINTEGRATION BLOCKED BY: #151"
        github.reviews[member.pr_number].append(
            {
                "id": 1000 + member.pr_number,
                "state": "COMMENTED",
                "commit_id": member.head,
                "submitted_at": NOW.isoformat(),
                "body": (
                    f"VERDICT: {verdict}\n"
                    f"<!-- dish-workstream-review:v1 workstream={WORKSTREAM} "
                    f"candidate={candidate.candidate_id} shape={candidate.shape_id} -->\n"
                    f"TESTS TO RUN: NONE.{extra}"
                ),
            }
        )


def test_body_only_integration_order_change_invalidates_review_and_forces_broad_recheck():
    github = GitHub()
    reviewed = _candidate()
    _review(github, reviewed)
    assert workstream.current_review_state(reviewed, github).status == "merge"

    unchanged = _candidate()
    assert unchanged.shape_id == reviewed.shape_id
    assert unchanged.candidate_id == reviewed.candidate_id
    assert workstream.current_review_state(unchanged, github).status == "merge"

    changed = _candidate(order_159="#151")
    assert [member.head for member in changed.members] == [member.head for member in reviewed.members]
    assert changed.shape_id != reviewed.shape_id
    assert changed.candidate_id != reviewed.candidate_id
    assert changed.members[2].integration_order == "#151"
    assert workstream.current_review_state(changed, github).status == "none"

    review_class, changed_prs, previous = workstream.recheck_scope(changed, github)
    assert review_class == "substantive"
    assert changed_prs == []
    assert previous == reviewed.candidate_id


def test_unchanged_order_is_idempotent_and_head_only_change_remains_focused():
    github = GitHub()
    reviewed = _candidate()
    _review(github, reviewed)

    moved = _candidate(head_157="d" * 40)
    assert moved.shape_id == reviewed.shape_id
    assert moved.candidate_id != reviewed.candidate_id
    review_class, changed_prs, previous = workstream.recheck_scope(moved, github)
    assert review_class == "focused"
    assert changed_prs == [157]
    assert previous == reviewed.candidate_id


def test_formal_review_cannot_introduce_different_unbound_integration_order():
    github = GitHub()
    candidate = _candidate()
    _review(github, candidate, divergent_order_pr=159)

    state = workstream.current_review_state(candidate, github)
    assert state.status == "partial"
    assert all(record.pr_number != 159 for record in state.records)
