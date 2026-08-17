"""Explicit post-merge Review request orchestration."""
from __future__ import annotations

from typing import Any

from pr_lifecycle_support import *
from pr_lifecycle_post_merge_dispatch import _dispatch_full_review
from pr_lifecycle_post_merge_review import (
    complete_obligation, ensure_corrective_owner, ensure_obligation, full_review_marker, matching_full_review, pr_link_marker,
)

def request_post_merge_review(
    engine,
    *,
    pr_number: int,
    thin_result: str,
    thin_summary: str,
    workspace: WorkspaceAgentDispatcher | None,
) -> dict[str, Any]:
    current = engine.inspect(engine.github.get_pr(pr_number))
    if current.state != LifecycleState.MERGED:
        raise LifecycleError(
            f"PR #{pr_number} is not authoritatively merged; ordinary Review lifecycle applies instead"
        )
    if engine.asana is None:
        raise LifecycleError("post-merge Review recovery requires Asana access for the durable obligation")
    obligation = ensure_obligation(
        asana=engine.asana,
        lifecycle=current,
        repository=engine.github.repository,
        thin_result=thin_result,
        thin_summary=thin_summary,
    )
    link = pr_link_marker(obligation=obligation)
    comments = engine.github.get_comments(current.number)
    if not any(link in str(comment.get("body") or "") for comment in comments):
        engine.github.add_comment(
            current.number,
            f"{link}\nExplicit post-merge Review recovery is bound to durable Asana obligation "
            f"`{obligation.task_gid}` for exact merged head `{obligation.head}`.\n\n"
            "— Dish PR lifecycle dispatcher",
        )
        comments = engine.github.get_comments(current.number)
        if not any(link in str(comment.get("body") or "") for comment in comments):
            raise LifecycleError("post-merge Review obligation GitHub linkage readback failed")

    review = matching_full_review(
        engine.github.get_reviews(current.number), obligation=obligation
    )
    corrective_gid = None
    if not obligation.completed and thin_result.strip().upper() == "SERIOUS DEFECT FOUND":
        corrective_gid = ensure_corrective_owner(asana=engine.asana, obligation=obligation)
    if review is not None and not obligation.completed:
        obligation, full_review_corrective_gid = complete_obligation(
            asana=engine.asana, obligation=obligation, review=review
        )
        corrective_gid = full_review_corrective_gid or corrective_gid

    dispatched = False
    dispatch_run_id = None
    dispatch_error = None
    if not obligation.completed and review is None:
        if workspace is None:
            dispatch_error = "published Review Workspace Agent trigger is not configured on this host"
        else:
            try:
                result = _dispatch_full_review(
                    workspace,
                    repository=engine.github.repository,
                    pr_number=current.number,
                    pr_url=current.url,
                    head=current.head,
                    obligation_key=obligation.key,
                    obligation_task_gid=obligation.task_gid,
                    marker=full_review_marker(key=obligation.key, head=obligation.head),
                )
                dispatched = True
                dispatch_run_id = result.run_id
            except LifecycleError as exc:
                dispatch_error = str(exc)

    return {
        "schema": "dish-post-merge-review-request-v1",
        "repository": engine.github.repository,
        "pr_number": current.number,
        "exact_merged_head": current.head,
        "thin_result": thin_result.strip().upper(),
        "thin_summary": thin_summary.strip(),
        "obligation": obligation.json(),
        "full_review_recorded": review is not None,
        "full_review_id": int(review["id"]) if review is not None else None,
        "full_review_verdict": str(review.get("verdict")) if review is not None else None,
        "corrective_task_gid": corrective_gid,
        "review_dispatched": dispatched,
        "review_dispatch_run_id": dispatch_run_id,
        "review_dispatch_error": dispatch_error,
    }
