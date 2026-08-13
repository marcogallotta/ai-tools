"""Local Integration execution for durable exact-head certification handoffs."""
from __future__ import annotations

import pr_gate
from pr_lifecycle_support import ImplementationFixDispatcher, LifecycleError, LifecycleState
from pr_lifecycle_helpers import local_work_from_review


class LocalIntegrationCertificationMixin:
    """Execute complete PR-local certification handoffs under bounded Integration."""

    local_integration_certifier: ImplementationFixDispatcher | None = None

    def dispatch_one(
        self,
        pr,
        *,
        workspace,
        local_reviewer,
        implementation_fixer=None,
        notify=None,
    ):
        current = self.inspect(self.github.get_pr(pr.number))
        certifier = self.local_integration_certifier
        if (
            current.state == LifecycleState.LOCAL_CERTIFICATION_REQUIRED
            and self.integration_authority
            and certifier is not None
            and certifier.command
        ):
            comments = self.github.get_comments(current.number)
            reviews = self.github.get_reviews(current.number)
            exact_review = pr_gate.latest_exact_head_review(reviews, reviewed_head=current.head)
            work_items = local_work_from_review(exact_review, comments, head=current.head)
            changed = False
            for work in work_items:
                if not work.completed:
                    changed = self._ensure_local_handoff(current, work) or changed
            if changed:
                current = self.inspect(self.github.get_pr(current.number))

            pending = [
                item for item in current.local_work
                if item.get("required") and not item.get("completed")
            ]
            if pending and all(item.get("handoff_present") for item in pending):
                work = pending[0]
                raw_pr = self.github.get_pr(current.number)
                reviews = self.github.get_reviews(current.number)
                exact_review = pr_gate.latest_exact_head_review(reviews, reviewed_head=current.head)
                context = {
                    "schema": "dish-pr-integration-certification-v1",
                    "repository": self.github.repository,
                    "pull_request": {
                        "number": current.number,
                        "url": current.url,
                        "branch": current.branch,
                        "head": current.head,
                        "base": current.base,
                        "body": str(raw_pr.get("body") or ""),
                    },
                    "task_ids": list(current.task_ids),
                    "review": exact_review,
                    "local_certification": dict(work),
                    "lifecycle": current.json(),
                    "instruction": (
                        "Act as Dish Integration. Re-read live GitHub/Asana authority and the current "
                        "Integration contract, execute the complete durable PR-local certification handoff "
                        "for this exact head, derive or safely create routine task/branch/agent identities "
                        "instead of asking Marco for them, and record durable exact-head pass/fail evidence."
                    ),
                }
                try:
                    certifier.dispatch(context)
                except LifecycleError as exc:
                    current.residual_reason = f"local Integration certification executor failed: {exc}"
                    current.human_action = None
                    return current

                reread = self.inspect(self.github.get_pr(current.number))
                if reread.head != current.head:
                    return reread
                if reread.state == LifecycleState.LOCAL_CERTIFICATION_REQUIRED:
                    reread.residual_reason = (
                        "local Integration certification executor returned without durable exact-head completion evidence"
                    )
                    reread.human_action = None
                    return reread
                if (
                    reread.state in {LifecycleState.INTEGRATION_READY, LifecycleState.MERGING}
                    and self.integration_authority
                    and self.integration_capable
                ):
                    return self._merge_exact_head(reread)
                return reread

        return super().dispatch_one(
            pr,
            workspace=workspace,
            local_reviewer=local_reviewer,
            implementation_fixer=implementation_fixer,
            notify=notify,
        )
