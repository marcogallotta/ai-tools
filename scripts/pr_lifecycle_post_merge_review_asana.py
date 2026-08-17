"""Asana-backed creation and lookup of post-merge Review obligations."""
from __future__ import annotations

from typing import Any, Mapping

from pr_lifecycle_owner import owning_task_identity_from_references
from pr_lifecycle_support import LifecycleError, PRLifecycle, LifecycleState
from pr_lifecycle_post_merge_review_types import (
    PostMergeAsana, PostMergeReviewObligation, THIN_RESULTS, _OBLIGATION_RE,
    new_obligation_key, obligation_marker,
)
from pr_lifecycle_post_merge_review_asana_io import _create_subtask, _list_subtasks


def _parse_obligation(task: Mapping[str, Any]) -> PostMergeReviewObligation | None:
    notes = str(task.get("notes") or "")
    match = _OBLIGATION_RE.search(notes)
    if match is None:
        return None
    thin = None
    for line in notes.splitlines():
        if line.startswith("Thin safety result: "):
            thin = line.split(":", 1)[1].strip()
            break
    gid = str(task.get("gid") or "")
    if not gid:
        raise LifecycleError("post-merge Review obligation task is missing its Asana GID")
    parent = task.get("parent")
    parent_gid = str(parent.get("gid")) if isinstance(parent, Mapping) and parent.get("gid") else ""
    return PostMergeReviewObligation(
        repository=match.group("repo"),
        pr_number=int(match.group("pr")),
        head=match.group("head").lower(),
        key=match.group("key").lower(),
        owner_task_gid=parent_gid,
        task_gid=gid,
        thin_result=thin,
        completed=bool(task.get("completed")),
        permalink_url=str(task.get("permalink_url")) if task.get("permalink_url") else None,
    )


def _matching_subtasks(
    asana: PostMergeAsana,
    *,
    owner_task_gid: str,
    repository: str,
    pr_number: int,
    head: str,
) -> list[PostMergeReviewObligation]:
    matches: list[PostMergeReviewObligation] = []
    for raw in _list_subtasks(asana, owner_task_gid):
        parsed = _parse_obligation(raw)
        if parsed is None:
            continue
        if (
            parsed.repository == repository
            and parsed.pr_number == pr_number
            and parsed.head == head.lower()
        ):
            if parsed.owner_task_gid and parsed.owner_task_gid != owner_task_gid:
                raise LifecycleError("post-merge Review obligation parent identity changed")
            if not parsed.owner_task_gid:
                parsed = PostMergeReviewObligation(
                    **{**parsed.json(), "owner_task_gid": owner_task_gid}
                )
            matches.append(parsed)
    return sorted(matches, key=lambda item: (item.completed, item.task_gid))


def find_obligation(
    *,
    asana: PostMergeAsana,
    lifecycle: PRLifecycle,
    repository: str,
) -> PostMergeReviewObligation | None:
    if lifecycle.state != LifecycleState.MERGED:
        return None
    owner, owner_error = owning_task_identity_from_references(lifecycle.task_ids)
    if owner_error or owner is None:
        return None
    matches = _matching_subtasks(
        asana,
        owner_task_gid=owner,
        repository=repository,
        pr_number=lifecycle.number,
        head=lifecycle.head,
    )
    if not matches:
        return None
    incomplete = [item for item in matches if not item.completed]
    if len(incomplete) > 1:
        raise LifecycleError(
            f"multiple incomplete post-merge Review obligations exist for PR #{lifecycle.number} exact head {lifecycle.head}"
        )
    return incomplete[0] if incomplete else matches[0]


def ensure_obligation(
    *,
    asana: PostMergeAsana,
    lifecycle: PRLifecycle,
    repository: str,
    thin_result: str,
    thin_summary: str,
) -> PostMergeReviewObligation:
    if lifecycle.state != LifecycleState.MERGED:
        raise LifecycleError("explicit post-merge Review recovery requires authoritative MERGED PR state")
    thin_result = thin_result.strip().upper()
    if thin_result not in THIN_RESULTS:
        raise LifecycleError(
            "thin safety result must be SAFE ENOUGH, SERIOUS DEFECT FOUND, or UNABLE TO DETERMINE"
        )
    owner, owner_error = owning_task_identity_from_references(lifecycle.task_ids)
    if owner_error or owner is None:
        raise LifecycleError(f"post-merge Review obligation requires one explicit owning Asana task: {owner_error}")
    asana.get_task(owner)
    existing = _matching_subtasks(
        asana,
        owner_task_gid=owner,
        repository=repository,
        pr_number=lifecycle.number,
        head=lifecycle.head,
    )
    incomplete = [item for item in existing if not item.completed]
    if len(incomplete) > 1:
        raise LifecycleError(
            f"multiple incomplete post-merge Review obligations already exist for exact head {lifecycle.head}"
        )
    if incomplete:
        return incomplete[0]

    key = new_obligation_key(repository, lifecycle.number, lifecycle.head)
    marker = obligation_marker(
        repository=repository, pr_number=lifecycle.number, head=lifecycle.head, key=key
    )
    summary = thin_summary.strip() or "No additional thin-pass summary was recorded."
    notes = (
        f"{marker}\n"
        "State: OPEN — FULL POST-MERGE REVIEW REQUIRED\n"
        "Owner: Review\n"
        f"Repository: {repository}\n"
        f"PR: #{lifecycle.number}\n"
        f"Exact merged PR head: {lifecycle.head}\n"
        f"Thin safety result: {thin_result}\n"
        f"Thin safety summary: {summary}\n\n"
        "Next action: perform full Review of this exact merged head through the existing Review mechanics. "
        "A pre-merge Review, a prior completed post-merge Review round, or later main movement does not satisfy this obligation. "
        "Keep this task incomplete until a formal exact-head post-merge Review carrying this round's matching obligation marker "
        "is durably recorded. VERDICT: BLOCK routes a bounded corrective Implementation owner; VERDICT: MERGE closes this obligation."
    )
    created = _create_subtask(
        asana, owner,
        name=f"Full post-merge Review — PR #{lifecycle.number} @ {lifecycle.head[:10]}",
        notes=notes,
    )
    created_parsed = _parse_obligation(created)
    if created_parsed is None:
        raise LifecycleError("created post-merge Review obligation did not read back its exact identity marker")
    if created_parsed.key != key or created_parsed.thin_result != thin_result:
        raise LifecycleError("created post-merge Review obligation did not read back the new Review round result")

    reread = _matching_subtasks(
        asana,
        owner_task_gid=owner,
        repository=repository,
        pr_number=lifecycle.number,
        head=lifecycle.head,
    )
    incomplete = [item for item in reread if not item.completed]
    if len(incomplete) != 1:
        raise LifecycleError(
            "post-merge Review obligation creation did not produce exactly one incomplete exact-head record"
        )
    if incomplete[0].key != key or incomplete[0].thin_result != thin_result:
        raise LifecycleError("post-merge Review obligation readback did not preserve the new Review round result")
    return incomplete[0]
