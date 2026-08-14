"""Draft-authoring continuation actions layered over the ordinary lifecycle dispatcher."""
from __future__ import annotations

from pr_lifecycle_support import *
from pr_lifecycle_helpers import _continuation_handoff_present, _continuation_key


class LifecycleAuthoringActionsMixin:
    def _ensure_implementation_continuation_handoff(
        self, pr: PRLifecycle, evidence: str
    ) -> bool:
        comments = self.github.get_comments(pr.number)
        if _continuation_handoff_present(comments, head=pr.head, evidence=evidence):
            return False
        key = _continuation_key(pr.head, evidence)
        marker = f"<!-- {IMPLEMENTATION_CONTINUATION_MARKER} head={pr.head} key={key} -->"
        self.github.add_comment(
            pr.number,
            f"{marker}\nIMPLEMENTATION CONTINUATION HANDOFF — exact head `{pr.head}`\n\n"
            f"Continue the existing Implementation branch/task and finish: {evidence}.\n\n"
            "This is the explicit continuation ownership handoff if the prior Implementation owner is unavailable. "
            "Keep this PR draft until authoring evidence is complete; do not route this work to Review, Integration, "
            "or local certification.\n\n— Dish PR lifecycle dispatcher",
        )
        return True

    def dispatch_one(
        self,
        pr: PRLifecycle,
        *,
        workspace: WorkspaceAgentDispatcher | None,
        local_reviewer: LocalReviewDispatcher | None,
        implementation_fixer: ImplementationFixDispatcher | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> PRLifecycle:
        notify = notify or (lambda _: None)
        current = self.inspect(self.github.get_pr(pr.number))
        if current.state != LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
            return super().dispatch_one(
                current,
                workspace=workspace,
                local_reviewer=local_reviewer,
                implementation_fixer=implementation_fixer,
                notify=notify,
            )

        evidence = current.authoring_evidence or "task-scoped authoring evidence"
        if any(lease.get("phase") in {"implementation", "fix"} for lease in current.active_leases):
            return current

        if implementation_fixer is None or not implementation_fixer.command:
            current.residual_reason = (
                f"implementation continuation consumer is unavailable; unfinished evidence: {evidence}"
            )
            current.human_action = f"PR #{current.number} still needs Implementation to finish {evidence}."
            self._notify_once(
                current,
                kind="implementation-continuation",
                action=evidence,
                message=current.human_action,
                notify=notify,
            )
            return current

        global_claim = self._implementation_claim_dispatch_context(current)
        self._ensure_implementation_continuation_handoff(current, evidence)
        reread = self.inspect(self.github.get_pr(current.number))
        if reread.head != current.head or reread.state != LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
            return reread

        lease_id = self._post_lease(reread, phase="implementation")
        claimed = self.inspect(self.github.get_pr(current.number))
        if claimed.head != current.head or claimed.state != LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
            self._release_lease(
                current.number,
                lease_id,
                reason="authoring state moved before Implementation continuation dispatch",
            )
            return claimed

        context = {
            "schema": "dish-pr-implementation-continuation-v1",
            "repository": self.github.repository,
            "pr_url": current.url,
            "pr_number": current.number,
            "branch": current.branch,
            "head": current.head,
            "task_ids": current.task_ids,
            "unfinished_authoring_evidence": evidence,
            "lifecycle": claimed.json(),
            "global_implementation_claim": global_claim,
            "instruction": (
                "Follow the current repository Implementation contract. Continue the existing draft PR, "
                "branch, and owning task; acquire or exact-generation-take over the supplied global Implementation claim "
                "before semantic work; finish only the named authoring evidence, update the durable PR "
                "evidence/head, and mark ready for review only when authoring is complete. Pending ordinary CI "
                "belongs to Integration and is not authoring evidence."
            ),
        }
        try:
            implementation_fixer.dispatch(context)
        except LifecycleError:
            self._release_lease(
                current.number,
                lease_id,
                reason="Implementation continuation dispatcher failed",
            )
            raise
        return self.inspect(self.github.get_pr(current.number))
