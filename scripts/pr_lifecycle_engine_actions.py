"""Idempotent review/local/integration dispatch actions."""
from __future__ import annotations

from pr_lifecycle_support import *
from pr_lifecycle_helpers import *
from pr_lifecycle_helpers import _handoff_key, _notice_key, _notice_present, _pr_number
from pr_lifecycle_operator import action_first_status
from pr_lifecycle_asana_writeback import reconcile_after_merge
from pr_lifecycle_ci_recovery import recover_failed_ci
from pr_lifecycle_host_routing import (
    CHATGPT_IMPLEMENTATION, LOCAL_IMPLEMENTATION, classify_requirement,
    implementation_host_for_boundary, implementation_host_for_review,
)
from pr_lifecycle_terminal import (
    TERMINAL_DISPOSITION_MARKER, TerminalCleanupDispatcher, asana_terminal_decision, cleanup_marker,
    comment_has_marker, disposition_marker,
)



def _fixer_command(dispatcher: Any, host: str) -> str | None:
    selector = getattr(dispatcher, "command_for", None)
    if callable(selector):
        return selector(host)
    # Legacy in-process dispatcher objects are interpreted as the remote/default path only.
    return getattr(dispatcher, "command", None) if host == CHATGPT_IMPLEMENTATION else None


def _dispatch_fixer(dispatcher: Any, context: dict[str, Any], *, host: str) -> None:
    selector = getattr(dispatcher, "command_for", None)
    if callable(selector):
        dispatcher.dispatch(context, host=host)
    else:
        if host != CHATGPT_IMPLEMENTATION:
            raise LifecycleError("legacy implementation/fix dispatcher is not classified for local execution")
        dispatcher.dispatch(context)


TERMINAL_RECOVERY_SLOT_SECONDS = 180

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
        if work.kind == "certification":
            label = "LOCAL INTEGRATION CERTIFICATION REQUIRED"
            role = "Integration"
        else:
            label = "LOCAL IMPLEMENTATION COMPLETION REQUIRED"
            role = "Implementation"
        gate_context = ""
        if (
            work.kind == "certification"
            and pr.gate
            and pr.gate.get("diagnosis") == pr_gate.GateDiagnosis.PENDING.value
        ):
            context = pr.gate.get("required_status_context") or pr_gate.REQUIRED_ORDINARY_CI_CONTEXT
            reason = pr.gate.get("reason") or "exact-head CI is still pending"
            gate_context = (
                f"Remaining exact-head CI gate: `{context}` — PENDING.\n"
                f"CI state: {reason}\n\n"
                "Integration owns both remaining gates on this exact head: run/poll the local certification "
                "and poll the CI gate. If either fails, record the exact evidence and route any semantic fix "
                "to Implementation.\n\n"
            )
        boundary = classify_requirement(work.instruction, default_kind=work.kind)
        route_note = ""
        if work.kind == "implementation":
            selected = implementation_host_for_boundary(boundary)
            if selected != LOCAL_IMPLEMENTATION:
                route_note = (
                    "REMOTE-FIRST ROUTING: local Implementation is not authorized by this text alone; "
                    "a local route requires an exact unavailable hosted capability plus bounded exhausted "
                    "fallbacks in the canonical IMPLEMENTATION / PUBLICATION classification.\n\n"
                )
        body = (
            f"{marker}\n{label} — exact head `{pr.head}`\n\n"
            f"Role: {role}\n\n"
            f"LOCAL WORK TYPE: {boundary.work_type}\n"
            f"LOCAL SCOPE: {boundary.scope}\n\n"
            f"Action: `{work.instruction}`\n\n"
            f"{route_note}"
            f"{gate_context}"
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
        notify(action_first_status(pr))
        return True

    def _finalize_authoritative_merge(
        self,
        source_before: PRLifecycle,
        *,
        raw_after_merge: dict[str, Any],
        merge_sha: str,
    ) -> PRLifecycle:
        """Reconcile only after authoritative GitHub MERGED readback from local Integration."""
        authoritative = self.inspect(raw_after_merge)
        authoritative.post_merge_gates = list(source_before.post_merge_gates)
        if authoritative.state != LifecycleState.MERGED:
            authoritative.state = LifecycleState.INTEGRATION_READY
            authoritative.state_label = STATE_LABELS[LifecycleState.INTEGRATION_READY]
            authoritative.residual_reason = (
                "local Integration returned but authoritative GitHub PR readback is not merged"
            )
            authoritative.human_action = None
            return authoritative
        merge_sha = str(merge_sha or "").lower()
        if FULL_SHA_RE.fullmatch(merge_sha) is None:
            authoritative.residual_reason = (
                "PR is authoritatively merged, but the authoritative merge commit SHA is unavailable; "
                "post-merge Asana landing reconciliation needs recovery"
            )
            return authoritative
        if self.asana is not None:
            try:
                writeback = reconcile_after_merge(
                    asana=self.asana,
                    lifecycle=authoritative,
                    repository=self.github.repository,
                    merge_sha=merge_sha,
                )
                if writeback.residual_gates:
                    authoritative.residual_reason = (
                        "source merged and Asana landing evidence is reconciled; residual gates remain: "
                        + ", ".join(writeback.residual_gates)
                    )
            except LifecycleError as exc:
                authoritative.residual_reason = f"PR merged; post-merge Asana writeback needs recovery: {exc}"
        return authoritative

    def _merge_exact_head(self, pr: PRLifecycle) -> PRLifecycle:
        """Legacy guard: V1-A forbids dispatcher/ChatGPT/broker-side landing."""
        current = self.inspect(self.github.get_pr(pr.number))
        current.residual_reason = (
            "dispatcher-side merge is disabled by Integration V1-A; final landing belongs only to the fenced "
            "local Claude/Codex Integration execution"
        )
        current.human_action = None
        return current

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
        # Replay missed post-merge Asana writeback before terminal cleanup.  Merge is
        # durable GitHub truth; controller restarts must converge even if the original
        # local Integration process died before writeback.
        if current.state == LifecycleState.MERGED and self.asana is not None:
            raw = self.github.get_pr(current.number)
            merge_sha = str(raw.get("merge_commit_sha") or "").lower()
            if FULL_SHA_RE.fullmatch(merge_sha):
                try:
                    reconcile_after_merge(
                        asana=self.asana, lifecycle=current,
                        repository=self.github.repository, merge_sha=merge_sha,
                    )
                except LifecycleError as exc:
                    current.residual_reason = f"PR merged; post-merge Asana writeback needs recovery: {exc}"
        current = recover_failed_ci(self, current)
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

            fix_host = implementation_host_for_review(exact_review if formal_block else None)
            selected_command = None if implementation_fixer is None else _fixer_command(implementation_fixer, fix_host)
            if selected_command is None:
                current.residual_reason = f"selected {fix_host} implementation/fix consumer is not configured"
                current.human_action = None
                return current

            lease_id = self._post_lease(current, phase="fix")

            reread = self.inspect(self.github.get_pr(current.number))
            if reread.head != current.head or reread.state != LifecycleState.CHANGES_REQUESTED:
                if lease_id is not None:
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
                "implementation_host": fix_host,
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
                _dispatch_fixer(implementation_fixer, context, host=fix_host)
            except LifecycleError:
                if lease_id is not None:
                    self._release_lease(
                        current.number, lease_id, reason="implementation/fix dispatcher failed"
                    )
                raise
            return self.inspect(self.github.get_pr(current.number))

        if current.state == LifecycleState.REVIEW_READY:
            review_class = current.review_class or "substantive"
            raw_pr = self.github.get_pr(current.number)
            review_host_witness = implementation_host_witness(
                raw_pr, self.github.get_comments(current.number), current_head=current.head, github=self.github
            )
            if (
                review_class in {"light", "focused", "mechanical"}
                and review_host_witness == CHATGPT_IMPLEMENTATION
                and local_reviewer
                and local_reviewer.command
            ):
                lease_id = self._post_lease(current, phase="review", review_class=review_class)
                review_context = current.json()
                review_context["review_execution"] = {
                    "role": "Review",
                    "host": "local",
                    "implementation_host_witness": review_host_witness,
                    "local_review_evidence_capable": True,
                    "routing": {
                        "review_evidence": "execute directly when within Review authority",
                        "semantic_fix": "Implementation",
                        "integration_action": "Integration",
                    },
                }
                try:
                    local_reviewer.dispatch(review_context)
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
                if pending[0]["kind"] == "certification":
                    action = (
                        f"give PR #{current.number} to a local Integration agent for exact-head certification; "
                        "full handoff is on the PR"
                    )
                    message = f"PR #{current.number} — REVIEW PASSED; local Integration certification required. Action: {action}"
                else:
                    action = f"give PR #{current.number} to a local Implementation agent; full handoff is on the PR"
                    message = f"PR #{current.number} — local Implementation completion required. Action: {action}"
                self._notify_once(
                    current,
                    kind=f"local-{pending[0]['kind']}",
                    action=action,
                    message=message,
                    notify=notify,
                )
            return current

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
        # Ordinary dispatch handles open PRs. Closed recovery is stateless but
        # rotates with the same 180-second cadence as the foreground watcher, so
        # fresh standalone processes cannot remain pinned to the newest page.
        values = self.status()
        candidate_reader = getattr(self.github, "closed_recovery_candidate", None)
        if callable(candidate_reader):
            slot = int(self.now().timestamp() // TERMINAL_RECOVERY_SLOT_SECONDS)
            candidate = candidate_reader(recovery_slot=slot)
            seen = {value.number for value in values}
            if candidate is not None and _pr_number(candidate) not in seen:
                values.append(self.inspect(candidate))
        elif include_closed:
            # Compatibility for test/third-party backends that have not yet
            # implemented the bounded page read. The repository adapter above
            # always takes the bounded path.
            seen = {value.number for value in values}
            values.extend(
                value for value in self.status(include_closed=True)
                if value.number not in seen
            )
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
