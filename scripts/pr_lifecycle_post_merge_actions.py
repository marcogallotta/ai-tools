"""Lifecycle interception for durable Review of explicitly requested merged PRs."""
from __future__ import annotations

from typing import Any

from pr_lifecycle_support import *
from pr_lifecycle_post_merge_dispatch import _dispatch_full_review
from pr_lifecycle_post_merge_request import request_post_merge_review
from pr_lifecycle_post_merge_review import (complete_obligation, find_obligation, full_review_marker, matching_full_review)


class PostMergeReviewActionsMixin:
    def _dispatch_post_merge_review(
        self,
        current: PRLifecycle,
        *,
        workspace: WorkspaceAgentDispatcher | None,
    ) -> PRLifecycle:
        if current.state != LifecycleState.MERGED or self.asana is None:
            return current
        try:
            obligation = find_obligation(
                asana=self.asana, lifecycle=current, repository=self.github.repository
            )
        except LifecycleError as exc:
            current.residual_reason = f"post-merge Review obligation needs recovery: {exc}"
            current.human_action = None
            return current
        if obligation is None or obligation.completed:
            return current

        review = matching_full_review(
            self.github.get_reviews(current.number), obligation=obligation
        )
        if review is not None:
            try:
                _, corrective_gid = complete_obligation(
                    asana=self.asana, obligation=obligation, review=review
                )
            except LifecycleError as exc:
                current.residual_reason = f"full post-merge Review is recorded but obligation writeback failed: {exc}"
                current.human_action = None
                return current
            if corrective_gid is not None:
                current.residual_reason = (
                    f"full post-merge Review BLOCK recorded for exact head {current.head}; "
                    f"corrective Implementation owner {corrective_gid} is open"
                )
            else:
                current.residual_reason = (
                    f"full post-merge Review MERGE verdict recorded for exact head {current.head}; "
                    "the durable full-review obligation is complete"
                )
            current.human_action = None
            return current

        current.residual_reason = (
            f"full post-merge Review obligation {obligation.task_gid} remains open for exact merged head {current.head}"
        )
        current.human_action = None
        if workspace is None:
            return current
        try:
            result = _dispatch_full_review(
                workspace,
                repository=self.github.repository,
                pr_number=current.number,
                pr_url=current.url,
                head=current.head,
                obligation_key=obligation.key,
                obligation_task_gid=obligation.task_gid,
                marker=full_review_marker(key=obligation.key, head=obligation.head),
            )
        except LifecycleError as exc:
            current.residual_reason = (
                f"full post-merge Review obligation {obligation.task_gid} remains open; Review dispatch failed: {exc}"
            )
            return current
        run = f" workspace run {result.run_id}" if result.run_id else ""
        current.residual_reason = (
            f"full post-merge Review obligation {obligation.task_gid} remains open; "
            f"existing Review mechanics dispatched{run}"
        )
        return current


    def request_post_merge_review(
        self,
        *,
        pr_number: int,
        thin_result: str,
        thin_summary: str,
        workspace: WorkspaceAgentDispatcher | None,
    ) -> dict[str, Any]:
        return request_post_merge_review(
            self,
            pr_number=pr_number,
            thin_result=thin_result,
            thin_summary=thin_summary,
            workspace=workspace,
        )

    def dispatch_one(
        self,
        pr: PRLifecycle,
        *,
        workspace: WorkspaceAgentDispatcher | None,
        local_reviewer: LocalReviewDispatcher | None,
        implementation_fixer: ImplementationFixDispatcher | None = None,
        terminal_cleaner=None,
        notify=None,
    ) -> PRLifecycle:
        current = self.inspect(self.github.get_pr(pr.number))
        post_merge_residual = None
        if current.state == LifecycleState.MERGED:
            current = self._dispatch_post_merge_review(current, workspace=workspace)
            post_merge_residual = current.residual_reason
        result = super().dispatch_one(
            current,
            workspace=workspace,
            local_reviewer=local_reviewer,
            implementation_fixer=implementation_fixer,
            terminal_cleaner=terminal_cleaner,
            notify=notify,
        )
        if post_merge_residual is not None:
            result.residual_reason = post_merge_residual
            result.human_action = None
        return result
