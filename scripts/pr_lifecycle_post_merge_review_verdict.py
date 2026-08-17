"""Formal Review matching and completion for post-merge obligations."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

import pr_gate
from pr_lifecycle_support import LifecycleError
from pr_lifecycle_post_merge_review_types import (
    CORRECTIVE_MARKER, CORRECTIVE_REVIEW_MARKER, OBLIGATION_CLOSE_MARKER,
    PostMergeAsana, PostMergeReviewObligation, _FULL_REVIEW_RE, full_review_marker,
)
from pr_lifecycle_post_merge_review_asana import _create_subtask, _list_subtasks, _parse_obligation

def matching_full_review(
    reviews: Iterable[Mapping[str, Any]],
    *,
    obligation: PostMergeReviewObligation,
) -> dict[str, Any] | None:
    candidates: list[tuple[str, int, dict[str, Any]]] = []
    expected_marker = full_review_marker(key=obligation.key, head=obligation.head)
    for raw in reviews:
        review = dict(raw)
        if str(review.get("commit_id") or review.get("commitId") or "").lower() != obligation.head:
            continue
        if str(review.get("state") or "").upper() not in {"COMMENT", "COMMENTED"}:
            continue
        body = str(review.get("body") or "")
        if expected_marker not in body:
            continue
        verdict = pr_gate.review_verdict(body)
        if verdict is None:
            continue
        marker_match = _FULL_REVIEW_RE.search(body)
        if marker_match is None or marker_match.group("key").lower() != obligation.key:
            continue
        try:
            review_id = int(review.get("id"))
        except (TypeError, ValueError):
            continue
        review["verdict"] = verdict
        review["id"] = review_id
        candidates.append((str(review.get("submitted_at") or review.get("submittedAt") or ""), review_id, review))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _story_has_marker(stories: Iterable[Mapping[str, Any]], marker: str) -> bool:
    return any(marker in str(story.get("text") or "") for story in stories)


def ensure_corrective_owner(
    *,
    asana: PostMergeAsana,
    obligation: PostMergeReviewObligation,
    review: Mapping[str, Any] | None = None,
) -> str:
    marker = (
        f"<!-- {CORRECTIVE_MARKER} repo={obligation.repository} pr={obligation.pr_number} "
        f"head={obligation.head} key={obligation.key} -->"
    )
    existing_gid = None
    for raw in _list_subtasks(asana, obligation.owner_task_gid):
        if marker in str(raw.get("notes") or ""):
            gid = str(raw.get("gid") or "")
            if not gid:
                raise LifecycleError("corrective owner matched but lacked an Asana GID")
            if bool(raw.get("completed")):
                raise LifecycleError("matching corrective Implementation owner is already complete; explicit recovery needs a live owner")
            existing_gid = gid
            break

    if existing_gid is None:
        source = (
            f"formal full post-merge Review id {int(review['id'])}"
            if review is not None
            else "bounded post-merge safety pass: SERIOUS DEFECT FOUND"
        )
        notes = (
            f"{marker}\n"
            "State: OPEN — CORRECTIVE IMPLEMENTATION REQUIRED\n"
            f"Source: {source} for {obligation.repository} PR #{obligation.pr_number}, "
            f"exact merged head {obligation.head}.\n\n"
            "Next action: Implementation reads the post-merge safety/full Review findings and implements the smallest "
            "coherent correction from exact current main through a new branch and PR. If reversal is appropriate, use "
            "the fail-closed landed-source recovery path. Database/runtime/deployment/external-effect recovery remains "
            "separate and must use its own authority."
        )
        created = _create_subtask(
            asana, obligation.owner_task_gid,
            name=f"Correct merged PR #{obligation.pr_number} Review findings @ {obligation.head[:10]}",
            notes=notes,
        )
        existing_gid = str(created.get("gid") or "")
        if not existing_gid or marker not in str(created.get("notes") or ""):
            raise LifecycleError("corrective Implementation owner creation readback failed")
        matches = [
            str(item.get("gid"))
            for item in _list_subtasks(asana, obligation.owner_task_gid)
            if marker in str(item.get("notes") or "") and item.get("gid") and not bool(item.get("completed"))
        ]
        if matches != [existing_gid]:
            raise LifecycleError("corrective Implementation owner did not dedupe to one incomplete exact-head task")

    if review is not None:
        review_id = int(review["id"])
        review_marker = (
            f"<!-- {CORRECTIVE_REVIEW_MARKER} key={obligation.key} head={obligation.head} "
            f"review={review_id} -->"
        )
        stories = asana.get_stories(existing_gid)
        if not _story_has_marker(stories, review_marker):
            asana.add_comment(
                existing_gid,
                f"{review_marker}\nFull post-merge Review `{review_id}` confirmed VERDICT: BLOCK for exact head "
                f"`{obligation.head}`. Use that formal Review as the corrective scope source.\n\n"
                "— Dish Agent: Review | automated lifecycle writeback",
            )
        if not _story_has_marker(asana.get_stories(existing_gid), review_marker):
            raise LifecycleError("corrective Implementation owner full-Review linkage readback failed")
    return existing_gid


def complete_obligation(
    *,
    asana: PostMergeAsana,
    obligation: PostMergeReviewObligation,
    review: Mapping[str, Any],
) -> tuple[PostMergeReviewObligation, str | None]:
    if obligation.completed:
        return obligation, None
    review_id = int(review["id"])
    verdict = str(review.get("verdict") or "")
    if verdict not in {"MERGE", "BLOCK"}:
        raise LifecycleError("post-merge full Review completion requires VERDICT: MERGE or VERDICT: BLOCK")
    marker = (
        f"<!-- {OBLIGATION_CLOSE_MARKER} key={obligation.key} head={obligation.head} "
        f"review={review_id} verdict={verdict} -->"
    )
    stories = asana.get_stories(obligation.task_gid)
    if not _story_has_marker(stories, marker):
        asana.add_comment(
            obligation.task_gid,
            f"{marker}\nFULL POST-MERGE REVIEW RECORDED — exact head `{obligation.head}` — "
            f"formal Review `{review_id}` — VERDICT: {verdict}.\n\n"
            "— Dish Agent: Review | automated lifecycle writeback",
        )
    if not _story_has_marker(asana.get_stories(obligation.task_gid), marker):
        raise LifecycleError("post-merge full Review verdict comment readback failed")

    corrective_gid = None
    if verdict == "BLOCK":
        corrective_gid = ensure_corrective_owner(asana=asana, obligation=obligation, review=review)
    asana.update_task_fields(obligation.task_gid, {"completed": True})
    task = asana.get_task(obligation.task_gid)
    if not bool(task.get("completed")):
        raise LifecycleError("post-merge full Review obligation completion readback failed")
    completed = _parse_obligation(task)
    if completed is None:
        raise LifecycleError("completed post-merge Review obligation lost its exact identity marker")
    if not completed.owner_task_gid:
        completed = PostMergeReviewObligation(
            **{**completed.json(), "owner_task_gid": obligation.owner_task_gid}
        )
    return completed, corrective_gid
