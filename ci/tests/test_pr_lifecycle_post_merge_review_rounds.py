from pathlib import Path
import sys

TESTS = Path(__file__).resolve().parent / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from post_merge_review_fakes import *


def _complete_merge_round(*, gh, asana, lifecycle):
    first = lifecycle.request_post_merge_review(
        pr_number=31,
        thin_result="SAFE ENOUGH",
        thin_summary="Initial bounded pass.",
        workspace=FakeWorkspace(),
    )
    key = first["obligation"]["key"]
    marker = post_merge.full_review_marker(key=key, head=HEAD)
    gh.reviews = [
        review(
            body=f"{marker}\nVERDICT: MERGE\nNo blocking findings.\nReviewed head: {HEAD}",
            review_id=51,
        )
    ]
    lifecycle._dispatch_post_merge_review(lifecycle.inspect(gh.pr), workspace=FakeWorkspace())
    assert asana.get_task(first["obligation"]["task_gid"])["completed"] is True
    return first, gh.reviews[0]


def test_completed_round_followed_by_serious_explicit_request_creates_fresh_round_and_corrective_owner():
    gh = FakeGitHub()
    asana = FakeAsana()
    lifecycle = engine(gh, asana)
    first, _ = _complete_merge_round(gh=gh, asana=asana, lifecycle=lifecycle)
    workspace = FakeWorkspace()

    second = lifecycle.request_post_merge_review(
        pr_number=31,
        thin_result="SERIOUS DEFECT FOUND",
        thin_summary="A new serious defect was found after the earlier full Review completed.",
        workspace=workspace,
    )

    records = obligation(asana)
    incomplete = [item for item in records if not item.completed]
    assert len(records) == 2
    assert len(incomplete) == 1
    assert incomplete[0].task_gid != first["obligation"]["task_gid"]
    assert incomplete[0].key != first["obligation"]["key"]
    assert incomplete[0].thin_result == "SERIOUS DEFECT FOUND"
    assert second["full_review_recorded"] is False
    assert second["review_dispatched"] is True
    assert workspace.calls[0]["obligation_key"] == incomplete[0].key
    corrective = [
        task for task in asana.list_subtasks(OWNER)
        if post_merge.CORRECTIVE_MARKER in str(task.get("notes") or "") and not task["completed"]
    ]
    assert len(corrective) == 1
    assert second["corrective_task_gid"] == corrective[0]["gid"]


def test_older_formal_review_cannot_close_fresh_round_for_same_merged_head():
    gh = FakeGitHub()
    asana = FakeAsana()
    lifecycle = engine(gh, asana)
    first, old_review = _complete_merge_round(gh=gh, asana=asana, lifecycle=lifecycle)

    second = lifecycle.request_post_merge_review(
        pr_number=31,
        thin_result="SAFE ENOUGH",
        thin_summary="Later explicit request still requires a fresh full Review.",
        workspace=FakeWorkspace(),
    )
    fresh = [item for item in obligation(asana) if not item.completed][0]

    assert fresh.key != first["obligation"]["key"]
    assert post_merge.matching_full_review([old_review], obligation=fresh) is None
    lifecycle._dispatch_post_merge_review(lifecycle.inspect(gh.pr), workspace=FakeWorkspace())
    assert asana.get_task(second["obligation"]["task_gid"])["completed"] is False


def test_retries_while_fresh_round_is_open_dedupe_to_one_incomplete_obligation():
    gh = FakeGitHub()
    asana = FakeAsana()
    lifecycle = engine(gh, asana)
    _complete_merge_round(gh=gh, asana=asana, lifecycle=lifecycle)
    gh.reviews = []
    workspace = FakeWorkspace()

    first = lifecycle.request_post_merge_review(
        pr_number=31,
        thin_result="SERIOUS DEFECT FOUND",
        thin_summary="Fresh Review round.",
        workspace=workspace,
    )
    second = lifecycle.request_post_merge_review(
        pr_number=31,
        thin_result="SERIOUS DEFECT FOUND",
        thin_summary="Retry of the same still-open round.",
        workspace=workspace,
    )

    records = obligation(asana)
    assert first["obligation"]["task_gid"] == second["obligation"]["task_gid"]
    assert first["obligation"]["key"] == second["obligation"]["key"]
    assert len(records) == 2
    assert sum(not item.completed for item in records) == 1
