"""Idempotent review/local/integration dispatch actions."""
from __future__ import annotations

from pr_lifecycle_support import *
from pr_lifecycle_helpers import *
from pr_lifecycle_helpers import _handoff_key, _notice_key, _notice_present
from pr_mutation_broker import (
    BrokerError, BrokerProofError, asana_task_allows_mutation, current_verified_grant, parse_request_comment, request_marker,
)
from pr_lifecycle_operator import action_first_status
from pr_lifecycle_owner import owning_task_identity_from_references
from pr_lifecycle_asana_writeback import reconcile_after_merge
from pr_lifecycle_terminal import (
    TERMINAL_DISPOSITION_MARKER, TerminalCleanupDispatcher, asana_terminal_decision, cleanup_marker,
    comment_has_marker, disposition_marker,
)

class LifecycleActionsMixin:
    def _broker_repository_id(self) -> int:
        value = getattr(self, "mutation_broker_repository_id", None)
        if value is None:
            getter = getattr(self.github, "get_repository_id", None)
            if getter is None:
                raise LifecycleError("mutation broker requires authoritative repository numeric identity")
            value = int(getter())
            self.mutation_broker_repository_id = value
        return int(value)

    def _broker_route(self, action: str) -> str | None:
        return str(getattr(self, "mutation_broker_routes", {}).get(action) or "") or None

    def _current_broker_grant(self, number: int):
        if not getattr(self, "mutation_broker_enabled", False):
            return None
        return current_verified_grant(
            github=self.github,
            pr_number=number,
            repository_id=self._broker_repository_id(),
        )

    def _broker_request_exists(
        self,
        pr: PRLifecycle,
        *,
        action: str,
        route: str,
        review_id: int | None,
        main_sha: str | None,
        grant_id: str | None = None,
        generation: int | None = None,
    ) -> bool:
        for comment in self.github.get_comments(pr.number):
            try:
                request = parse_request_comment(comment)
            except BrokerError:
                continue
            if (
                request.action == action
                and request.task_gid in pr.task_ids
                and request.pr_number == pr.number
                and request.branch == pr.branch
                and request.head == pr.head
                and request.review_id == review_id
                and request.main_sha == main_sha
                and request.route == route
                and request.grant_id == grant_id
                and request.generation == generation
            ):
                return True
        return False

    def _submit_broker_request(
        self,
        pr: PRLifecycle,
        *,
        action: str,
        route: str,
        review_id: int | None = None,
        main_sha: str | None = None,
        grant_id: str | None = None,
        generation: int | None = None,
        authority_id: str | None = None,
    ) -> bool:
        owner, owner_error = owning_task_identity_from_references(pr.task_ids)
        if owner_error or owner is None:
            raise LifecycleError(f"mutation broker requires one explicit owning Asana task: {owner_error}")
        if self._broker_request_exists(
            pr, action=action, route=route, review_id=review_id, main_sha=main_sha,
            grant_id=grant_id, generation=generation,
        ):
            return False
        marker = request_marker(
            request_id=str(uuid.uuid4()),
            action=action,
            task_gid=owner,
            pr_number=pr.number,
            branch=pr.branch,
            head=pr.head,
            review_id=review_id,
            main_sha=main_sha,
            grant_id=grant_id,
            generation=generation,
            route=route,
            authority_id=authority_id,
        )
        self.github.add_comment(
            pr.number,
            f"{marker}\nMUTATION ADMISSION REQUEST — `{action}` for exact head `{pr.head}`. "
            "The request is only an optimistic precondition; the default-branch broker must re-read GitHub, "
            "Asana and standing role authority before any grant.\n\n— Dish PR lifecycle dispatcher",
        )
        return True

    def _broker_grant_for(self, pr: PRLifecycle, *, action: str, route: str):
        grant = self._current_broker_grant(pr.number)
        if grant is None or grant.closed:
            return None
        if grant.is_stale(self.now()):
            raise LifecycleError(
                "current mutation grant is stale; it cannot mutate and cannot transfer by age alone (takeover/recovery required)"
            )
        owner, owner_error = owning_task_identity_from_references(pr.task_ids)
        if owner_error or owner is None:
            raise LifecycleError(f"cannot validate mutation grant owner: {owner_error}")
        if (
            grant.action != action
            or grant.pr_number != pr.number
            or grant.task_gid != owner
            or grant.branch != pr.branch
            or grant.starting_head != pr.head
            or grant.route != route
        ):
            raise LifecycleError(
                f"active mutation grant is incompatible with requested {action} route/head; current grant is "
                f"{grant.action} generation {grant.generation} on {grant.starting_head}"
            )
        return grant

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
        body = (
            f"{marker}\n{label} — exact head `{pr.head}`\n\n"
            f"Role: {role}\n\n"
            f"Action: `{work.instruction}`\n\n"
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

        broker_grant = None
        if getattr(self, "mutation_broker_enabled", False):
            route = self._broker_route("merge")
            if not route:
                current.residual_reason = "mutation broker is active but no Integration merge route is configured"
                current.human_action = None
                return current
            try:
                broker_grant = self._broker_grant_for(current, action="merge", route=route)
            except LifecycleError as exc:
                current.residual_reason = str(exc)
                current.human_action = None
                return current
            if broker_grant is None:
                current.residual_reason = "merge requires a current proven mutation broker grant"
                current.human_action = None
                return current
            # Consequential merge boundary: re-read live Asana immediately before merge.
            owner, owner_error = owning_task_identity_from_references(current.task_ids)
            if owner_error or owner is None or self.asana is None:
                current.residual_reason = "merge cannot re-read the explicit owning Asana task"
                current.human_action = None
                return current
            try:
                task = self.asana.get_task(owner)
            except LifecycleError as exc:
                current.residual_reason = f"merge live Asana read failed: {exc}"
                current.human_action = None
                return current
            allowed, reason = asana_task_allows_mutation(task)
            if not allowed:
                current.residual_reason = f"merge stopped by live Asana authority: {reason}"
                current.human_action = None
                return current
            live_main = self.github.get_ref_sha(f"heads/{current.base}")
            if broker_grant.main_sha != live_main:
                current.residual_reason = "merge broker grant is stale against current target main; reclassify/re-request"
                current.human_action = None
                return current
        else:
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
        # Re-read after grant/legacy lease validation. A semantic head move returns to Review.
        reread = self.inspect(self.github.get_pr(pr.number))
        if reread.head != pr.head:
            return reread
        if reread.state not in {LifecycleState.MERGING, LifecycleState.INTEGRATION_READY}:
            return reread
        if getattr(self, "mutation_broker_enabled", False):
            route = self._broker_route("merge")
            assert route is not None
            # Final proof/grant read at the irreversible boundary. Missing/expired/deleted
            # proof cannot fall back to commenter identity or an older cached grant.
            final_grant = self._broker_grant_for(reread, action="merge", route=route)
            if final_grant is None or final_grant.grant_id != broker_grant.grant_id:
                reread.residual_reason = "merge broker grant changed/disappeared at final boundary"
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
        raw_after_merge = self.github.get_pr(pr.number)
        authoritative = self.inspect(raw_after_merge)
        # A merged PR no longer needs Review parsing for source truth, but the exact-head
        # Review remains the durable authority for residual post-merge acceptance gates.
        authoritative.post_merge_gates = list(current.post_merge_gates)
        if authoritative.state != LifecycleState.MERGED:
            authoritative.state = LifecycleState.INTEGRATION_READY
            authoritative.state_label = STATE_LABELS[LifecycleState.INTEGRATION_READY]
            authoritative.residual_reason = "merge API returned success but authoritative PR readback is not merged"
            return authoritative
        merge_sha = str(result.get("sha") or raw_after_merge.get("merge_commit_sha") or "").lower()
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
        if getattr(self, "mutation_broker_enabled", False) and broker_grant is not None:
            # Closing the grant is itself broker-proven; this comment only requests that
            # state transition and cannot free the grant by itself.
            try:
                self._submit_broker_request(
                    authoritative,
                    action="complete",
                    route=broker_grant.route,
                    grant_id=broker_grant.grant_id,
                    generation=broker_grant.generation,
                )
            except LifecycleError:
                # GitHub MERGED readback remains authoritative source truth. A failed close
                # request is recovery debt and may not turn a real merge back into unmerged.
                prior = authoritative.residual_reason
                close_reason = "mutation-grant close request needs recovery"
                authoritative.residual_reason = f"{prior}; {close_reason}" if prior else f"PR merged; {close_reason}"
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
            if active_fix and not getattr(self, "mutation_broker_enabled", False):
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

            broker_grant = None
            broker_route = None
            lease_id = None
            if getattr(self, "mutation_broker_enabled", False):
                broker_route = self._broker_route("fix")
                if not broker_route:
                    current.residual_reason = "mutation broker is active but no Implementation/fix route is configured"
                    current.human_action = None
                    return current
                try:
                    broker_grant = self._broker_grant_for(current, action="fix", route=broker_route)
                except LifecycleError as exc:
                    current.residual_reason = str(exc)
                    current.human_action = None
                    return current
                if broker_grant is None:
                    review_id = None
                    if exact_review is not None:
                        try:
                            review_id = int(exact_review.get("id"))
                        except (TypeError, ValueError):
                            current.residual_reason = "current formal Review lacks numeric id required for brokered fix eligibility"
                            return current
                    self._submit_broker_request(
                        current, action="fix", route=broker_route, review_id=review_id
                    )
                    reread = self.inspect(self.github.get_pr(current.number))
                    reread.residual_reason = "Review/CI fix is eligible; mutation request is waiting for the serialized broker"
                    reread.human_action = None
                    return reread
            else:
                lease_id = self._post_lease(current, phase="fix")

            reread = self.inspect(self.github.get_pr(current.number))
            if reread.head != current.head or reread.state != LifecycleState.CHANGES_REQUESTED:
                if lease_id is not None:
                    self._release_lease(
                        current.number, lease_id, reason="exact blocked head moved before fix dispatch"
                    )
                return reread
            if getattr(self, "mutation_broker_enabled", False):
                assert broker_route is not None
                broker_grant = self._broker_grant_for(reread, action="fix", route=broker_route)
                if broker_grant is None:
                    reread.residual_reason = "broker grant disappeared before Implementation/fix dispatch"
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
                if lease_id is not None:
                    self._release_lease(
                        current.number, lease_id, reason="implementation/fix dispatcher failed"
                    )
                raise
            return self.inspect(self.github.get_pr(current.number))

        if current.state == LifecycleState.REVIEW_READY:
            review_class = current.review_class or "substantive"
            if review_class in {"light", "focused", "mechanical"} and local_reviewer and local_reviewer.command:
                lease_id = self._post_lease(current, phase="review", review_class=review_class)
                review_context = current.json()
                review_context["review_execution"] = {
                    "role": "Review",
                    "host": "local",
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

        if (
            current.state == LifecycleState.REVIEW_PASSED
            and getattr(self, "mutation_broker_enabled", False)
            and self.integration_authority
            and any(
                token in str(current.residual_reason or "").lower()
                for token in ("mergeab", "integration ordering", "base", "conflict")
            )
        ):
            route = self._broker_route("integration-reconcile")
            reconciler = getattr(self, "integration_reconciler", None)
            if not route:
                current.residual_reason = "bounded Integration reconciliation is required but no broker route is configured"
                current.human_action = None
                return current
            if reconciler is None or not reconciler.command:
                current.residual_reason = "bounded Integration reconciliation is required but no Integration consumer is configured"
                current.human_action = None
                return current
            reviews = self.github.get_reviews(current.number)
            exact_review = pr_gate.latest_exact_head_review(reviews, reviewed_head=current.head)
            try:
                review_id = int(exact_review.get("id")) if exact_review is not None else None
            except (TypeError, ValueError):
                review_id = None
            live_main = self.github.get_ref_sha(f"heads/{current.base}")
            try:
                grant = self._broker_grant_for(current, action="integration-reconcile", route=route)
            except LifecycleError as exc:
                current.residual_reason = str(exc)
                current.human_action = None
                return current
            if grant is None:
                self._submit_broker_request(
                    current,
                    action="integration-reconcile",
                    route=route,
                    review_id=review_id,
                    main_sha=live_main,
                )
                reread = self.inspect(self.github.get_pr(current.number))
                reread.residual_reason = "bounded Integration reconciliation request is waiting for the serialized broker"
                reread.human_action = None
                return reread
            # Re-read PR/review/task/proof immediately before the first head-changing
            # reconciliation consumer is allowed to acquire its branch/worktree.
            reread = self.inspect(self.github.get_pr(current.number))
            if reread.head != current.head or reread.state != LifecycleState.REVIEW_PASSED:
                return reread
            grant = self._broker_grant_for(reread, action="integration-reconcile", route=route)
            if grant is None:
                reread.residual_reason = "reconciliation grant disappeared before Integration consumer dispatch"
                return reread
            context = {
                "schema": "dish-pr-integration-reconcile-v1",
                "repository": self.github.repository,
                "pr_url": reread.url,
                "pr_number": reread.number,
                "branch": reread.branch,
                "reviewed_head": reread.head,
                "review_id": review_id,
                "main_sha": live_main,
                "task_ids": reread.task_ids,
                "mutation_grant": {
                    "grant_id": grant.grant_id,
                    "generation": grant.generation,
                    "consumer_id": grant.consumer_id,
                    "route": grant.route,
                    "starting_head": grant.starting_head,
                    "event_comment_id": grant.event_comment_id,
                },
                "lifecycle": reread.json(),
                "instruction": (
                    "Act as Dish Integration. Preserve only already-authorized outcomes whose combined result is "
                    "mechanically/intentionally determined. Do not make a product, architecture, workflow-policy, "
                    "PostgreSQL/schema, test-weakening, or other semantic choice. Re-read PR open/head, Review, live "
                    "Asana, current main, route and broker proof before first mutation and publication. Any ambiguity "
                    "returns to Implementation. Any content-changing result is a new head and requires fresh independent Review."
                ),
            }
            reconciler.dispatch(context)
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

        if current.state in {LifecycleState.INTEGRATION_READY, LifecycleState.MERGING} and self.integration_authority and self.integration_capable:
            if getattr(self, "mutation_broker_enabled", False):
                route = self._broker_route("merge")
                if not route:
                    current.residual_reason = "mutation broker is active but no Integration merge route is configured"
                    current.human_action = None
                    return current
                reviews = self.github.get_reviews(current.number)
                exact_review = pr_gate.latest_exact_head_review(reviews, reviewed_head=current.head)
                try:
                    review_id = int(exact_review.get("id")) if exact_review is not None else None
                except (TypeError, ValueError):
                    review_id = None
                live_main = self.github.get_ref_sha(f"heads/{current.base}")
                try:
                    grant = self._broker_grant_for(current, action="merge", route=route)
                except LifecycleError as exc:
                    current.residual_reason = str(exc)
                    current.human_action = None
                    return current
                if grant is None:
                    self._submit_broker_request(
                        current, action="merge", route=route, review_id=review_id, main_sha=live_main
                    )
                    reread = self.inspect(self.github.get_pr(current.number))
                    reread.residual_reason = "Review accepted the exact head; merge mutation request is waiting for the serialized broker"
                    reread.human_action = None
                    return reread
            elif current.state == LifecycleState.MERGING and not any(
                lease.get("phase") == "integration" and lease.get("owner") == DISPATCH_OWNER
                for lease in current.active_leases
            ):
                return current
            result = self._merge_exact_head(current)
            if result.state == LifecycleState.MERGED:
                notify(action_first_status(result))
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
