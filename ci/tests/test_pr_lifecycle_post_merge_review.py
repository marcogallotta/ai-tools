from pathlib import Path
import sys

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from post_merge_review_fakes import *

def test_explicit_merged_review_creates_durable_obligation_before_full_dispatch_and_dedupes():
    gh = FakeGitHub()
    asana = FakeAsana()
    workspace = FakeWorkspace()
    lifecycle = engine(gh, asana)

    first = lifecycle.request_post_merge_review(
        pr_number=31,
        thin_result="SAFE ENOUGH",
        thin_summary="No immediate release-blocking defect found in the bounded pass.",
        workspace=workspace,
    )
    second = lifecycle.request_post_merge_review(
        pr_number=31,
        thin_result="SAFE ENOUGH",
        thin_summary="Repeated explicit request.",
        workspace=workspace,
    )

    records = obligation(asana)
    assert len(records) == 1
    assert records[0].completed is False
    assert first["obligation"]["task_gid"] == second["obligation"]["task_gid"]
    assert first["review_dispatched"] is True
    assert len(workspace.calls) == 2  # identical trigger identity; remote API idempotency owns retry dedupe
    assert workspace.calls[0]["obligation_key"] == workspace.calls[1]["obligation_key"]
    assert post_merge.PR_LINK_MARKER in gh.comments[0]["body"]


def test_thin_safe_result_stays_open_when_review_dispatch_is_unavailable():
    gh = FakeGitHub()
    asana = FakeAsana()
    result = engine(gh, asana).request_post_merge_review(
        pr_number=31,
        thin_result="SAFE ENOUGH",
        thin_summary="Bounded pass only.",
        workspace=FakeWorkspace(fail=True),
    )

    assert result["review_dispatched"] is False
    assert "workspace unavailable" in result["review_dispatch_error"]
    assert obligation(asana)[0].completed is False


def test_historical_premerge_review_does_not_satisfy_post_merge_obligation():
    gh = FakeGitHub()
    gh.reviews = [review(body=f"VERDICT: MERGE\nReviewed head: {HEAD}")]
    asana = FakeAsana()
    workspace = FakeWorkspace()

    result = engine(gh, asana).request_post_merge_review(
        pr_number=31,
        thin_result="UNABLE TO DETERMINE",
        thin_summary="Full pass required.",
        workspace=workspace,
    )

    assert result["full_review_recorded"] is False
    assert obligation(asana)[0].completed is False
    assert len(workspace.calls) == 1


def test_matching_full_post_merge_review_closes_exact_obligation():
    gh = FakeGitHub()
    asana = FakeAsana()
    lifecycle = engine(gh, asana)
    request = lifecycle.request_post_merge_review(
        pr_number=31,
        thin_result="SAFE ENOUGH",
        thin_summary="Bounded pass only.",
        workspace=FakeWorkspace(),
    )
    key = request["obligation"]["key"]
    marker = post_merge.full_review_marker(key=key, head=HEAD)
    gh.reviews = [review(body=f"{marker}\nVERDICT: MERGE\nNo blocking findings.\nReviewed head: {HEAD}")]

    current = lifecycle.inspect(gh.pr)
    reconciled = lifecycle._dispatch_post_merge_review(current, workspace=FakeWorkspace())

    record = obligation(asana)[0]
    assert record.completed is True
    assert "obligation is complete" in reconciled.residual_reason
    stories = asana.get_stories(record.task_gid)
    assert any(post_merge.OBLIGATION_CLOSE_MARKER in story["text"] for story in stories)


def test_full_post_merge_block_creates_bounded_corrective_implementation_owner():
    gh = FakeGitHub()
    asana = FakeAsana()
    lifecycle = engine(gh, asana)
    request = lifecycle.request_post_merge_review(
        pr_number=31,
        thin_result="SERIOUS DEFECT FOUND",
        thin_summary="Potential unsafe source behavior found.",
        workspace=FakeWorkspace(),
    )
    corrective_before = [
        task for task in asana.list_subtasks(OWNER)
        if post_merge.CORRECTIVE_MARKER in str(task.get("notes") or "")
    ]
    assert len(corrective_before) == 1
    assert corrective_before[0]["completed"] is False
    corrective_gid = corrective_before[0]["gid"]

    key = request["obligation"]["key"]
    marker = post_merge.full_review_marker(key=key, head=HEAD)
    gh.reviews = [
        review(
            body=(
                f"{marker}\nVERDICT: BLOCK\nRequired change: reverse unsafe source behavior through normal Implementation.\n"
                f"Reviewed head: {HEAD}"
            ),
            review_id=44,
        )
    ]

    reconciled = lifecycle._dispatch_post_merge_review(lifecycle.inspect(gh.pr), workspace=FakeWorkspace())

    assert obligation(asana)[0].completed is True
    corrective = [
        task for task in asana.list_subtasks(OWNER)
        if post_merge.CORRECTIVE_MARKER in str(task.get("notes") or "")
    ]
    assert len(corrective) == 1
    assert corrective[0]["gid"] == corrective_gid
    assert corrective[0]["completed"] is False
    corrective_stories = asana.get_stories(corrective_gid)
    assert any(post_merge.CORRECTIVE_REVIEW_MARKER in story["text"] for story in corrective_stories)
    assert "corrective Implementation owner" in reconciled.residual_reason


def test_later_main_identity_is_not_used_to_replace_merged_review_head():
    gh = FakeGitHub()
    gh.pr["base"]["sha"] = "d" * 40  # later main movement is context only
    asana = FakeAsana()

    result = engine(gh, asana).request_post_merge_review(
        pr_number=31,
        thin_result="SAFE ENOUGH",
        thin_summary="Exact merged content remains the candidate.",
        workspace=FakeWorkspace(),
    )

    assert result["exact_merged_head"] == HEAD
    assert result["obligation"]["head"] == HEAD


def test_unmerged_pr_uses_ordinary_review_path_instead():
    gh = FakeGitHub()
    gh.pr["merged"] = False
    gh.pr["merged_at"] = None
    gh.pr["state"] = "open"
    asana = FakeAsana()

    with pytest.raises(pr_lifecycle.LifecycleError, match="ordinary Review lifecycle"):
        engine(gh, asana).request_post_merge_review(
            pr_number=31,
            thin_result="SAFE ENOUGH",
            thin_summary="not applicable",
            workspace=FakeWorkspace(),
        )
