"""Draft-authoring continuation actions layered over the ordinary lifecycle dispatcher."""
from __future__ import annotations

from pr_lifecycle_support import *
from pr_lifecycle_helpers import _continuation_handoff_present, _continuation_key
from pr_lifecycle_post_merge_actions import PostMergeReviewActionsMixin


class LifecycleAuthoringActionsMixin(PostMergeReviewActionsMixin):
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
        terminal_cleaner=None,
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
                terminal_cleaner=terminal_cleaner,
                notify=notify,
            )

        evidence = current.authoring_evidence or "task-scoped authoring evidence"
        if not getattr(self, "mutation_broker_enabled", False) and any(
            lease.get("phase") in {"implementation", "fix"} for lease in current.active_leases
        ):
            return current

        broker_grant = None
        broker_route = None
        if getattr(self, "mutation_broker_enabled", False):
            broker_route = self._broker_route("implementation")
            if not broker_route:
                current.residual_reason = "mutation broker is active but no Implementation continuation route is configured"
                current.human_action = None
                return current
            try:
                broker_grant = self._broker_grant_for(current, action="implementation", route=broker_route)
            except LifecycleError as exc:
                current.residual_reason = str(exc)
                current.human_action = None
                return current
            if broker_grant is None:
                self._submit_broker_request(current, action="implementation", route=broker_route)
                reread = self.inspect(self.github.get_pr(current.number))
                reread.residual_reason = "Implementation continuation mutation request is waiting for the serialized broker"
                reread.human_action = None
                return reread

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

        self._ensure_implementation_continuation_handoff(current, evidence)
        reread = self.inspect(self.github.get_pr(current.number))
        if reread.head != current.head or reread.state != LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
            return reread

        lease_id = None
        if not getattr(self, "mutation_broker_enabled", False):
            lease_id = self._post_lease(reread, phase="implementation")
        claimed = self.inspect(self.github.get_pr(current.number))
        if claimed.head != current.head or claimed.state != LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
            if lease_id is not None:
                self._release_lease(
                    current.number,
                    lease_id,
                    reason="authoring state moved before Implementation continuation dispatch",
                )
            return claimed

        # Re-verify the exact proven grant immediately before dispatch; a stale/deleted
        # proof is a fail-closed recovery boundary rather than permission to continue.
        if getattr(self, "mutation_broker_enabled", False):
            assert broker_route is not None
            broker_grant = self._broker_grant_for(claimed, action="implementation", route=broker_route)
            if broker_grant is None:
                claimed.residual_reason = "Implementation continuation broker grant disappeared before dispatch"
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
            "mutation_grant": (
                None
                if broker_grant is None
                else {
                    "grant_id": broker_grant.grant_id,
                    "generation": broker_grant.generation,
                    "consumer_id": broker_grant.consumer_id,
                    "route": broker_grant.route,
                    "starting_head": broker_grant.starting_head,
                    "event_comment_id": broker_grant.event_comment_id,
                }
            ),
            "instruction": (
                "Follow the current repository Implementation contract. Continue the existing draft PR, "
                "branch, and owning task; finish only the named authoring evidence, update the durable PR "
                "evidence/head, and mark ready for review only when authoring is complete. Pending ordinary CI "
                "belongs to Integration and is not authoring evidence."
            ),
        }
        try:
            implementation_fixer.dispatch(context)
        except LifecycleError:
            if lease_id is not None:
                self._release_lease(
                    current.number,
                    lease_id,
                    reason="Implementation continuation dispatcher failed",
                )
            raise
        return self.inspect(self.github.get_pr(current.number))
