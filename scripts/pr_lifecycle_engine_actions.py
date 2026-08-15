"""Idempotent review/local/integration dispatch actions."""
from __future__ import annotations

from pr_lifecycle_support import *
from pr_lifecycle_helpers import *
from pr_lifecycle_helpers import _handoff_key, _notice_key, _notice_present
from pr_lifecycle_terminal import (
    TERMINAL_DISPOSITION_MARKER, TerminalCleanupDispatcher, asana_terminal_decision, cleanup_marker,
    comment_has_marker, disposition_marker,
)

class LifecycleActionsMixin:
    def _terminal_cleanup(
        self,
        current: PRLifecycle,
        *,
        disposition: str,
        terminal_cleaner: TerminalCleanupDispatcher | None,
        notify: Callable[[str], None],
    ) -> PRLifecycle:
        if not current.branch.startswith("agent/"):
            current.residual_reason = f"terminal branch {current.branch!r} is not an agent/* branch; cleanup refused"
            current.human_action = "inspect terminal lineage manually; default/protected/unrelated refs are never auto-deleted"
            return current
        comments = self.github.get_comments(current.number)
        marker = cleanup_marker(current, disposition)
        if comment_has_marker(comments, marker):
            return current
        branch = self.github.get_branch(current.branch)
        if branch is not None and bool(branch.get("protected")):
            current.residual_reason = f"terminal agent branch {current.branch!r} is protected; cleanup refused"
            current.human_action = "resolve protected-branch policy manually without deleting an unrelated ref"
            return current
        if terminal_cleaner is None:
            current.residual_reason = "terminal cleanup dispatcher is not configured"
            current.human_action = "configure repository-owned terminal cleanup before retrying lifecycle dispatch"
            return current
        try:
            result = terminal_cleaner.dispatch(current, disposition)
        except LifecycleError as exc:
            current.residual_reason = str(exc)
            current.human_action = "recover/preserve the reported terminal lineage, then retry cleanup"
            self._notify_once(
                current,
                kind="terminal-cleanup",
                action=str(exc),
                message=f"PR #{current.number} — terminal cleanup refused. Action: {exc}",
                notify=notify,
            )
            return current
        if result.get("remote_branch_removed") is not True:
            raise LifecycleError("terminal cleanup did not confirm remote branch removal")
        self.github.add_comment(
            current.number,
            f"{marker}\nTerminal cleanup verified for `{current.branch}` at exact head `{current.head}`. "
            f"Local state present: `{bool(result.get('local_state_present'))}`.\n\n— Dish PR lifecycle dispatcher",
        )
        reread = self.github.get_comments(current.number)
        if not comment_has_marker(reread, marker):
            raise LifecycleError("terminal cleanup succeeded but durable GitHub cleanup marker readback failed")
        return self.inspect(self.github.get_pr(current.number))

    def _dispatch_terminal(
        self,
        current: PRLifecycle,
        *,
        terminal_cleaner: TerminalCleanupDispatcher | None,
        notify: Callable[[str], None],
    ) -> PRLifecycle | None:
        disposition: str | None = None
        if current.state == LifecycleState.MERGED:
            disposition = "merged"
        elif current.state == LifecycleState.CLOSED:
            # Closed-unmerged is itself authoritative terminal state. Preserve a prior
            # explicit superseded/abandoned disposition marker when present.
            comments = self.github.get_comments(current.number)
            for candidate in ("superseded", "abandoned"):
                token = f"{TERMINAL_DISPOSITION_MARKER} disposition={candidate} head={current.head}"
                if comment_has_marker(comments, token):
                    disposition = candidate
                    break
            disposition = disposition or "closed"
        else:
            decision = asana_terminal_decision(current)
            if decision is None:
                return None
            marker = disposition_marker(decision, current)
            comments = self.github.get_comments(current.number)
            if not comment_has_marker(comments, marker):
                lineage = []
                if decision.replacement_pr is not None:
                    lineage.append(f"replacement PR #{decision.replacement_pr}")
                if decision.replacement_task is not None:
                    lineage.append(f"replacement task {decision.replacement_task}")
                linkage = f" Replacement: {', '.join(lineage)}." if lineage else ""
                self.github.add_comment(
                    current.number,
                    f"{marker}\nAuthoritative terminal disposition: **{decision.disposition}**. "
                    f"Source: Asana task `{decision.task_gid}` — {decision.reason}.{linkage}\n\n"
                    "— Dish PR lifecycle dispatcher",
                )
                comments = self.github.get_comments(current.number)
                if not comment_has_marker(comments, marker):
                    raise LifecycleError("terminal disposition comment write readback failed; PR left open")
            reread_pr = self.github.get_pr(current.number)
            if pr_gate.pr_head_sha(reread_pr) != current.head:
                return self.inspect(reread_pr)
            self.github.close_pr(current.number)
            closed = self.inspect(self.github.get_pr(current.number))
            if closed.state != LifecycleState.CLOSED:
                raise LifecycleError("GitHub close request did not produce authoritative closed-unmerged readback")
            current = closed
            disposition = decision.disposition
        return self._terminal_cleanup(
            current, disposition=disposition, terminal_cleaner=terminal_cleaner, notify=notify
        )

    def _post_lease(self, pr: PRLifecycle, *, phase: str, review_class: str | None = None) -> str:
        lease_id = str(uuid.uuid4())
        class_field = f" class={review_class}" if review_class else ""
        marker = (
            f"<!-- {LEASE_MARKER} phase={phase} head={pr.head} lease={lease_id} "
            f"owner={DISPATCH_OWNER}{class_field} -->"
        )
        line = f"{phase.upper()} CLAIMED — head {pr.head} — stale after 60m without structured renewal/activity."
        self.github.add_comment(pr.number, f"{marker}\n{line}\n\n— Dish PR lifecycle dispatcher")
        return lease_id

    def _release_lease(self, number: int, lease_id: str, *, reason: str) -> None:
        marker = f"<!-- {LEASE_RELEASE_MARKER} lease={lease_id} -->"
        self.github.add_comment(number, f"{marker}\nLease released: {reason}\n\n— Dish PR lifecycle dispatcher")

    def _ensure_local_handoff(self, pr: PRLifecycle, work: LocalWork) -> bool:
        if work.handoff_present or not work.instruction:
            return False
        key = _handoff_key(work.kind, pr.head, work.instruction)
        marker = f"<!-- {LOCAL_HANDOFF_MARKER} kind={work.kind} head={pr.head} key={key} -->"
        label = "LOCAL CERTIFICATION REQUIRED" if work.kind == "certification" else "LOCAL IMPLEMENTATION COMPLETION REQUIRED"
        body = (
            f"{marker}\n{label} — exact head `{pr.head}`\n\n"
            f"Action: `{work.instruction}`\n\n"
            "This handoff is exact-head scoped. A head change invalidates it and requires the normal review/recheck path.\n\n"
            "— Dish PR lifecycle dispatcher"
        )
        self.github.add_comment(pr.number, body)
        return True

    def _notify_once(
        self,
        pr: PRLifecycle,
        *,
        kind: str,
        action: str,
        message: str,
        notify: Callable[[str], None],
    ) -> bool:
        comments = self.github.get_comments(pr.number)
        if _notice_present(comments, kind=kind, head=pr.head, action=action):
            return False
        key = _notice_key(kind, pr.head, action)
        marker = f"<!-- {HUMAN_NOTICE_MARKER} kind={kind} head={pr.head} key={key} -->"
        self.github.add_comment(
            pr.number,
            f"{marker}\nHuman action notice recorded for exact head `{pr.head}`.\n\n— Dish PR lifecycle dispatcher",
        )
        notify(message)
        return True

    def _merge_exact_head(self, pr: PRLifecycle) -> PRLifecycle:
        current = self.inspect(self.github.get_pr(pr.number))
        if current.head != pr.head:
            return current
        if current.state == LifecycleState.MERGED:
            return current
        if current.state not in {LifecycleState.INTEGRATION_READY, LifecycleState.MERGING}:
            return current
        if not self.integration_authority or not self.integration_capable:
            return current

        active_dispatcher_lease = next(
            (
                lease
                for lease in current.active_leases
                if lease["phase"] == "integration" and lease.get("owner") == DISPATCH_OWNER
            ),
            None,
        )
        if active_dispatcher_lease is None:
            self._post_lease(current, phase="integration")
        # Re-read after creating/resuming the lease. A semantic head move returns to Review.
        reread = self.inspect(self.github.get_pr(pr.number))
        if reread.head != pr.head:
            return reread
        if reread.state not in {LifecycleState.MERGING, LifecycleState.INTEGRATION_READY}:
            return reread
        try:
            result = self.github.merge(
                pr.number, expected_head=pr.head, method=self.merge_method
            )
        except HTTPError as exc:
            # Capability/branch-protection failures are residual Integration boundaries.
            if exc.status in {401, 403, 405, 409, 422}:
                reread = self.inspect(self.github.get_pr(pr.number))
                if reread.state == LifecycleState.MERGED:
                    return reread
                reread.state = LifecycleState.INTEGRATION_READY
                reread.state_label = STATE_LABELS[LifecycleState.INTEGRATION_READY]
                reread.residual_reason = f"merge capability/authorization failed: {exc}"
                reread.human_action = "use an authorized Integration host or resolve the reported merge boundary"
                return reread
            raise
        if result.get("merged") is not True:
            reread = self.inspect(self.github.get_pr(pr.number))
            if reread.state == LifecycleState.MERGED:
                return reread
            reread.state = LifecycleState.INTEGRATION_READY
            reread.state_label = STATE_LABELS[LifecycleState.INTEGRATION_READY]
            reread.residual_reason = f"GitHub merge did not confirm success: {result.get('message') or result!r}"
            return reread
        # Never infer MERGED from the merge response alone; authoritative PR readback is required.
        authoritative = self.inspect(self.github.get_pr(pr.number))
        if authoritative.state != LifecycleState.MERGED:
            authoritative.state = LifecycleState.INTEGRATION_READY
            authoritative.state_label = STATE_LABELS[LifecycleState.INTEGRATION_READY]
            authoritative.residual_reason = "merge API returned success but authoritative PR readback is not merged"
        return authoritative

    def dispatch_one(
        self,
        pr: PRLifecycle,
        *,
        workspace: WorkspaceAgentDispatcher | None,
        local_reviewer: LocalReviewDispatcher | None,
        implementation_fixer: ImplementationFixDispatcher | None = None,
        terminal_cleaner: TerminalCleanupDispatcher | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> PRLifecycle:
        notify = notify or (lambda _: None)
        current = self.inspect(self.github.get_pr(pr.number))
        terminal = self._dispatch_terminal(current, terminal_cleaner=terminal_cleaner, notify=notify)
        if terminal is not None:
            return terminal

        if current.state == LifecycleState.CHANGES_REQUESTED:
            active_fix = any(
                lease.get("phase") in {"fix", "implementation"}
                for lease in current.active_leases
            )
            if active_fix:
                return current

            reviews = self.github.get_reviews(current.number)
            exact_review = pr_gate.latest_exact_head_review(reviews, reviewed_head=current.head)
            formal_block = exact_review is not None and exact_review.get("verdict") == "BLOCK"
            pr_owned_ci_failure = bool(
                current.gate
                and current.gate.get("diagnosis")
                == pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value
            )
            if not formal_block and not pr_owned_ci_failure:
                return self.inspect(self.github.get_pr(current.number))

            if implementation_fixer is None or not implementation_fixer.command:
                current.residual_reason = "implementation/fix dispatcher is not configured"
                current.human_action = "configure the existing implementation/fix consumer command"
                self._notify_once(
                    current,
                    kind="fix-dispatch-config",
                    action=current.human_action,
                    message=(
                        f"PR #{current.number} — fix dispatch unavailable. "
                        "Action: configure the existing implementation/fix consumer command."
                    ),
                    notify=notify,
                )
                return current

            lease_id = self._post_lease(current, phase="fix")
            reread = self.inspect(self.github.get_pr(current.number))
            if reread.head != current.head or reread.state != LifecycleState.CHANGES_REQUESTED:
                self._release_lease(
                    current.number, lease_id, reason="exact blocked head moved before fix dispatch"
                )
                return reread

            context = {
                "schema": "dish-pr-fix-dispatch-v1",
                "repository": self.github.repository,
                "pr_url": current.url,
                "pr_number": current.number,
                "branch": current.branch,
                "blocked_head": current.head,
                "task_ids": current.task_ids,
                "formal_block_review": exact_review if formal_block else None,
                "pr_owned_ci_failure": current.gate if pr_owned_ci_failure else None,
                "lifecycle": reread.json(),
                "instruction": (
                    "Follow the current repository Implementation contract. Update the existing PR/branch, "
                    "treat blocked_head as the exact review identity, re-read GitHub before semantic work, "
                    + (
                        "fix the PR-owned exact-head required CI failure, and return the new exact PR head; "
                        "any semantic head movement requires substantive re-review."
                        if pr_owned_ci_failure and not formal_block
                        else "and return the new exact PR head for the reviewer's requested disposition."
                    )
                ),
            }
            try:
                implementation_fixer.dispatch(context)
            except LifecycleError:
                self._release_lease(
                    current.number, lease_id, reason="implementation/fix dispatcher failed"
                )
                raise
            return self.inspect(self.github.get_pr(current.number))

        if current.state == LifecycleState.REVIEW_READY:
            review_class = current.review_class or "substantive"
            if review_class in {"light", "focused", "mechanical"} and local_reviewer and local_reviewer.command:
                lease_id = self._post_lease(current, phase="review", review_class=review_class)
                try:
                    local_reviewer.dispatch(current.json())
                except LifecycleError:
                    self._release_lease(current.number, lease_id, reason="bounded local reviewer failed")
                    raise
                return self.inspect(self.github.get_pr(current.number))

            if workspace is None:
                current.residual_reason = "ChatGPT Review dispatch adapter is not configured"
                current.human_action = "configure the published Review Workspace Agent trigger"
                self._notify_once(
                    current,
                    kind="review-dispatch-config",
                    action=current.human_action,
                    message=f"PR #{current.number} — review dispatch unavailable. Action: configure the published Review Workspace Agent trigger.",
                    notify=notify,
                )
                return current
            try:
                result = workspace.dispatch(
                    repository=self.github.repository,
                    pr_number=current.number,
                    pr_url=current.url,
                    head=current.head,
                    review_class=review_class,
                    task_ids=current.task_ids,
                )
            except LifecycleError as exc:
                current.residual_reason = str(exc)
                current.human_action = str(exc)
                self._notify_once(
                    current,
                    kind="review-dispatch-error",
                    action=str(exc),
                    message=f"PR #{current.number} — review dispatch unavailable. Action: {exc}.",
                    notify=notify,
                )
                return current
            lease_id = str(uuid.uuid5(uuid.NAMESPACE_URL, result.idempotency_key))
            marker = (
                f"<!-- {LEASE_MARKER} phase=review head={current.head} lease={lease_id} "
                f"owner={DISPATCH_OWNER} class={review_class} -->"
            )
            run_line = f" Workspace run `{result.run_id}`." if result.run_id else ""
            self.github.add_comment(
                current.number,
                f"{marker}\nREVIEW DISPATCHED — head {current.head} — class {review_class}.{run_line} "
                "Lease is advisory and stale after 60m without structured renewal/activity.\n\n"
                "— Dish PR lifecycle dispatcher",
            )
            return self.inspect(self.github.get_pr(current.number))

        if current.state in {
            LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED,
            LifecycleState.LOCAL_CERTIFICATION_REQUIRED,
        }:
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
            # Notification occurs only after durable handoff is confirmed by re-read.
            pending = [item for item in current.local_work if item.get("required") and not item.get("completed")]
            if pending and all(item.get("handoff_present") for item in pending):
                kind = "local certification" if pending[0]["kind"] == "certification" else "local implementation completion"
                action = pending[0].get("instruction") or "follow the PR handoff"
                self._notify_once(
                    current,
                    kind=f"local-{pending[0]['kind']}",
                    action=action,
                    message=f"PR #{current.number} — {kind} required. Action: {action}",
                    notify=notify,
                )
            return current

        if current.state in {LifecycleState.INTEGRATION_READY, LifecycleState.MERGING} and self.integration_authority and self.integration_capable:
            if current.state == LifecycleState.MERGING and not any(
                lease.get("phase") == "integration" and lease.get("owner") == DISPATCH_OWNER
                for lease in current.active_leases
            ):
                return current
            result = self._merge_exact_head(current)
            if result.state == LifecycleState.MERGED:
                notify(f"PR #{result.number} — merged.")
                if terminal_cleaner is not None:
                    return self._terminal_cleanup(
                        result, disposition="merged", terminal_cleaner=terminal_cleaner, notify=notify
                    )
            return result
        return current

    def dispatch(
        self,
        *,
        include_closed: bool = False,
        workspace: WorkspaceAgentDispatcher | None = None,
        local_reviewer: LocalReviewDispatcher | None = None,
        implementation_fixer: ImplementationFixDispatcher | None = None,
        terminal_cleaner: TerminalCleanupDispatcher | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> list[PRLifecycle]:
        values = self.status(include_closed=include_closed)
        results: list[PRLifecycle] = []
        for value in values:
            results.append(
                self.dispatch_one(
                    value,
                    workspace=workspace,
                    local_reviewer=local_reviewer,
                    implementation_fixer=implementation_fixer,
                    terminal_cleaner=terminal_cleaner,
                    notify=notify,
                )
            )
        return results
