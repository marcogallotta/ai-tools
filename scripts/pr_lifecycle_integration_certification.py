"""Local-only Integration dispatch for certification, reconciliation, and landing."""
from __future__ import annotations

from typing import Any
import time

import pr_gate
from pr_lifecycle_local_integration import (
    HANDOFF_SCHEMA,
    MAX_INTEGRATION_ATTEMPTS,
    LocalIntegrationFence,
    finalize_attempt_result,
    find_handoff,
    handoff_key,
    load_attempt_result,
    marker,
    transient_infrastructure_reason,
)
from pr_lifecycle_support import FULL_SHA_RE, LifecycleError, LifecycleState, STATE_LABELS
from pr_lifecycle_helpers import local_work_from_review


TARGET_RECOVERY_MARKER = "dish-integration-target-recovery:v1"


class LocalIntegrationCertificationMixin:
    """Dispatch exact-head Integration work only to the configured local launcher."""

    local_integration_launcher: Any | None = None

    @staticmethod
    def _integration_reconciliation_required(current) -> bool:
        if current.state != LifecycleState.REVIEW_PASSED:
            return False
        residual = str(current.residual_reason or "").lower()
        return any(
            token in residual
            for token in ("mergeab", "integration ordering", "base", "conflict", "target-specific", "landing target")
        )

    @staticmethod
    def _review_id(exact_review: dict[str, Any] | None) -> int | None:
        if exact_review is None:
            return None
        try:
            value = int(exact_review.get("id"))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _ultimate_target_branch(raw_pr: dict[str, Any]) -> str:
        base = raw_pr.get("base")
        if isinstance(base, dict):
            repo = base.get("repo")
            if isinstance(repo, dict):
                default_branch = str(repo.get("default_branch") or "").strip()
                if default_branch:
                    return default_branch
            base_ref = str(base.get("ref") or "").strip()
            if base_ref:
                return base_ref
        return "main"

    def _target_landing_proof(self, raw_pr: dict[str, Any]) -> dict[str, Any]:
        target_branch = self._ultimate_target_branch(raw_pr)
        target_sha = self.github.get_ref_sha(f"heads/{target_branch}")
        base = raw_pr.get("base")
        immediate_base = str(base.get("ref") or "") if isinstance(base, dict) else ""
        merged = bool(raw_pr.get("merged") or raw_pr.get("merged_at"))
        merge_effect_sha = str(raw_pr.get("merge_commit_sha") or "").lower()
        if not merged:
            return {
                "landed": False,
                "target_branch": target_branch,
                "target_sha": target_sha,
                "immediate_base": immediate_base,
                "effect_sha": None,
                "reason": "pull request has no authoritative merge effect yet",
            }
        if FULL_SHA_RE.fullmatch(merge_effect_sha) is None:
            return {
                "landed": False,
                "target_branch": target_branch,
                "target_sha": target_sha,
                "immediate_base": immediate_base,
                "effect_sha": None,
                "reason": "GitHub merged=true lacks an exact merge_commit_sha for target-specific proof",
            }
        comparison = None
        last_error = None
        for attempt in range(1, 4):
            try:
                _, _, comparison = self.github.http.request(
                    "GET",
                    self.github._url(f"compare/{merge_effect_sha}...{target_sha}"),
                    headers=self.github.headers,
                )
                break
            except LifecycleError as exc:
                last_error = exc
                lowered = str(exc).lower()
                transient = any(
                    token in lowered
                    for token in (
                        "http 429", "http 500", "http 502", "http 503", "http 504",
                        "timed out", "timeout", "connection reset", "temporary",
                        "service unavailable", "bad gateway", "gateway timeout",
                    )
                )
                if not transient or attempt == 3:
                    raise LifecycleError(
                        f"target-specific GitHub readback failed after {attempt}/3 typed attempts: {exc}"
                    ) from exc
                time.sleep(0.15 * (2 ** (attempt - 1)))
        if not isinstance(comparison, dict):
            raise LifecycleError(
                f"GitHub target-specific compare response was not an object: {last_error}"
            )
        compare_status = str(comparison.get("status") or "").lower()
        landed = compare_status in {"ahead", "identical"}
        return {
            "landed": landed,
            "target_branch": target_branch,
            "target_sha": target_sha,
            "immediate_base": immediate_base,
            "effect_sha": merge_effect_sha,
            "compare_status": compare_status,
            "reason": (
                f"merge effect {merge_effect_sha} is contained by refs/heads/{target_branch} at {target_sha}"
                if landed
                else (
                    f"GitHub merged=true only proves composition into {immediate_base or '(unknown base)'}; "
                    f"merge effect {merge_effect_sha} is not contained by intended target "
                    f"refs/heads/{target_branch} at {target_sha}"
                )
            ),
        }

    def inspect(self, pr):
        lifecycle = super().inspect(pr)
        if not bool(pr.get("merged") or pr.get("merged_at")):
            return lifecycle
        try:
            proof = self._target_landing_proof(pr)
        except LifecycleError as exc:
            lifecycle.state = LifecycleState.WAITING_INFRASTRUCTURE
            lifecycle.state_label = STATE_LABELS[LifecycleState.WAITING_INFRASTRUCTURE]
            lifecycle.residual_reason = f"target-specific landing readback unavailable: {exc}"
            lifecycle.human_action = None
            return lifecycle
        if proof["landed"]:
            return lifecycle
        lifecycle.state = LifecycleState.REVIEW_PASSED
        lifecycle.state_label = STATE_LABELS[LifecycleState.REVIEW_PASSED]
        lifecycle.residual_reason = (
            "target-specific source landing incomplete: "
            f"{proof['reason']}; routine base/target recovery is an Integration responsibility"
        )
        lifecycle.human_action = None
        return lifecycle

    def _ensure_local_integration_handoff(self, current) -> dict[str, Any]:
        reviews = self.github.get_reviews(current.number)
        exact_review = pr_gate.latest_exact_head_review(reviews, reviewed_head=current.head)
        review_id = self._review_id(exact_review)
        if exact_review is None or review_id is None or str(exact_review.get("verdict")) != "MERGE":
            raise LifecycleError("local Integration handoff requires one authoritative exact-head MERGE review")
        raw_pr = self.github.get_pr(current.number)
        target_branch = self._ultimate_target_branch(raw_pr)
        main_sha = self.github.get_ref_sha(f"heads/{target_branch}")
        key = handoff_key(
            repository=self.github.repository,
            pr_number=current.number,
            branch=current.branch,
            head=current.head,
            review_id=review_id,
            main_sha=main_sha,
        )
        comments = self.github.get_comments(current.number)
        existing = find_handoff(
            comments,
            head=current.head,
            key=key,
            main_sha=main_sha,
            review_id=review_id,
        )
        if existing is None:
            handoff_marker = marker(
                head=current.head,
                key=key,
                main_sha=main_sha,
                review_id=review_id,
            )
            mode = "mechanical reconciliation + landing" if self._integration_reconciliation_required(current) else "landing"
            task_line = ", ".join(current.task_ids) if current.task_ids else "(missing)"
            recovery_marker = (
                f"<!-- {TARGET_RECOVERY_MARKER} source_pr={current.number} "
                f"source_head={current.head} target={target_branch} -->"
            )
            body = (
                f"{handoff_marker}\n"
                f"LOCAL INTEGRATION V1-A HANDOFF — exact reviewed head `{current.head}`\n\n"
                f"Mode: {mode}\n"
                f"PR: #{current.number} `{current.branch}` -> immediate base `{current.base}`\n"
                f"Intended landing target: `refs/heads/{target_branch}`\n"
                f"Owning Asana task(s): {task_line}\n"
                f"Exact Review: `{review_id}` verdict `MERGE`\n"
                f"Observed intended target `{target_branch}` at handoff creation: `{main_sha}`\n\n"
                "This is a local-only Integration handoff. Final Integration/merge must run on a local "
                "Claude/Codex host with a live checkout and real Git/worktree tooling. There is no ChatGPT, "
                "connector or GitHub Actions landing fallback.\n\n"
                "Before the first mutation and again immediately before the irreversible merge boundary, re-read "
                "the live GitHub PR/head/base/Review, the intended target ref, and the explicit owning Asana task. "
                "Fetch current origin state. Use expected-head protection for publication/merge and require "
                "target-specific authoritative readback; GitHub merged=true on an intermediate stacked base is "
                "a composition event, not terminal source landing.\n\n"
                "Only conflict-free/mechanical reconciliation already determined by current authority is allowed. "
                "A semantic/product/schema/policy/test-weakening choice stops and returns to Implementation. Any "
                "content-changing reconciliation creates a new PR head and must stop for fresh independent exact-head "
                "Review before landing.\n\n"
                "If this PR was already merged only to an intermediate stacked branch, mechanically recover without "
                "Marco relay only when the intended target is a proven prefix and this exact reviewed candidate is the "
                "ordered missing semantic suffix. Publish one recovery PR to the intended target, carry the same owning "
                f"task IDs and source lineage, include this exact marker in its body: `{recovery_marker}`, and STOP for "
                "fresh exact-head Review of that recovery PR. If exact equivalence cannot be proved, return one typed "
                "blocker instead of guessing.\n\n"
                "The launcher receives a repository-owned per-PR/head claim. The OS lock is consequential-mutation "
                "admission; the JSON checkpoint and attempt artifacts are crash/compaction recovery evidence. "
                "Checkpoint certifying/reconciling/reconciled/premerge/head-changed/failed-evidence/merged with "
                "`scripts/pr_lifecycle.py integration-checkpoint`.\n\n"
                "— Dish PR lifecycle dispatcher"
            )
            self.github.add_comment(current.number, body)
            comments = self.github.get_comments(current.number)
            existing = find_handoff(
                comments,
                head=current.head,
                key=key,
                main_sha=main_sha,
                review_id=review_id,
            )
            if existing is None:
                raise LifecycleError("local Integration handoff comment write readback failed")
        try:
            comment_id = int(existing.get("id"))
        except (TypeError, ValueError) as exc:
            raise LifecycleError("local Integration handoff comment has no authoritative numeric id") from exc
        return {
            "comment_id": comment_id,
            "key": key,
            "main_sha": main_sha,
            "target_branch": target_branch,
            "review_id": review_id,
            "review": exact_review,
        }

    def _dispatch_local_certification(self, current):
        launcher = self.local_integration_launcher
        if launcher is None or not launcher.command:
            return None
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

        pending = [item for item in current.local_work if item.get("required") and not item.get("completed")]
        if not pending or not all(item.get("handoff_present") for item in pending):
            return current
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
                "Act as local Dish Integration. Re-read live GitHub/Asana authority and the current Integration "
                "contract, execute the complete durable PR-local certification handoff for this exact head with "
                "real local tooling, and record durable exact-head pass/fail evidence. Certification success does "
                "not authorize this dispatcher to merge; final landing is a separate fenced local Integration run."
            ),
        }
        try:
            launcher.dispatch(context)
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

    def _target_recovery_pr(self, current, target_branch: str) -> dict[str, Any] | None:
        token = (
            f"<!-- {TARGET_RECOVERY_MARKER} source_pr={current.number} "
            f"source_head={current.head} target={target_branch} -->"
        )
        matches = []
        for candidate in self.github.list_prs():
            body = str(candidate.get("body") or "")
            base = candidate.get("base")
            base_ref = str(base.get("ref") or "") if isinstance(base, dict) else ""
            if token in body and base_ref == target_branch:
                matches.append(candidate)
        if len(matches) > 1:
            raise LifecycleError("multiple Integration target-recovery PRs match the exact source identity")
        return matches[0] if matches else None

    @staticmethod
    def _cleanup_deferable(reason: str | None) -> bool:
        value = str(reason or "").lower()
        return bool(value and ("checked out" in value or "worktree" in value))

    def _finalize_landed_attempt(
        self,
        current,
        raw_after,
        result,
        proof,
        *,
        terminal_cleaner=None,
        notify=None,
    ):
        classified = finalize_attempt_result(
            result,
            outcome="LANDED",
            retryable=False,
            reason=str(proof["reason"]),
            target_proof=proof,
            next_owner="Lifecycle controller",
            next_action="continue post-merge reconciliation/cleanup and activation readback separately",
        )
        reread = self.inspect(raw_after)
        if reread.state != LifecycleState.MERGED:
            raise LifecycleError("target proof succeeded but lifecycle did not converge to MERGED")
        merge_sha = str(raw_after.get("merge_commit_sha") or "").lower()
        finalized = self._finalize_authoritative_merge(
            current,
            raw_after_merge=raw_after,
            merge_sha=merge_sha,
        )
        finalized.residual_reason = (
            f"target-specific landing proven on refs/heads/{proof['target_branch']} at {proof['target_sha']}; "
            f"attempt {classified['attempt_id']}"
        )
        finalized.human_action = None
        if notify is not None:
            from pr_lifecycle_operator import action_first_status
            notify(action_first_status(finalized))
        if terminal_cleaner is None or finalized.state != LifecycleState.MERGED:
            return finalized
        cleaned = self._terminal_cleanup(
            finalized,
            disposition="merged",
            terminal_cleaner=terminal_cleaner,
            notify=notify or (lambda _: None),
        )
        if cleaned.state == LifecycleState.MERGED and self._cleanup_deferable(cleaned.residual_reason):
            cleaned.residual_reason = (
                "MERGED; cleanup deferred to lifecycle controller: "
                + str(cleaned.residual_reason)
            )
            cleaned.human_action = None
        return cleaned

    def _consume_previous_attempt(
        self,
        current,
        fence: LocalIntegrationFence,
        prior: dict[str, Any],
        *,
        terminal_cleaner=None,
        notify=None,
    ) -> tuple[Any, bool]:
        live = fence.liveness()
        if live["running"]:
            current.state = LifecycleState.MERGING
            current.state_label = STATE_LABELS[LifecycleState.MERGING]
            pid_text = f"worker={live['worker_pid']} child={live['child_pid']}"
            current.residual_reason = (
                f"RUNNING — real flock held and live process witness present ({pid_text}); "
                "controller reconciliation continues independently"
            )
            current.human_action = None
            return current, False
        if live["lock_held"]:
            current.state = LifecycleState.WAITING_INFRASTRUCTURE
            current.state_label = STATE_LABELS[LifecycleState.WAITING_INFRASTRUCTURE]
            current.residual_reason = (
                "Integration flock is held but no live worker/child PID witness is available; "
                "RUNNING is not claimed and no duplicate mutation starts"
            )
            current.human_action = None
            return current, False

        result = load_attempt_result(prior)
        generation = int(prior.get("generation") or 0)
        if result is None and prior.get("attempt_id"):
            raw_path = prior.get("attempt_result_path")
            if raw_path:
                from pathlib import Path
                from pr_lifecycle_local_integration import ATTEMPT_RESULT_SCHEMA, _atomic_write, _now
                result_path = Path(str(raw_path)).expanduser().resolve()
                result = {
                    "schema": ATTEMPT_RESULT_SCHEMA,
                    "attempt_id": prior.get("attempt_id"),
                    "claim_id": prior.get("claim_id"),
                    "generation": generation,
                    "repository": prior.get("repository"),
                    "task_ids": list(prior.get("task_ids") or []),
                    "pull_request": {
                        "number": prior.get("pr_number"),
                        "branch": prior.get("branch"),
                        "head": prior.get("head"),
                    },
                    "target": {
                        "branch": prior.get("target_branch"),
                        "observed_sha": prior.get("main_sha"),
                    },
                    "started_at": prior.get("worker_started_at") or prior.get("acquired_at"),
                    "finished_at": _now(),
                    "process_exit_code": None,
                    "stdout_path": prior.get("stdout_path"),
                    "stderr_path": prior.get("stderr_path"),
                    "result_path": str(result_path),
                    "outcome": "INTERRUPTED",
                    "retryable": True,
                    "reason": "prior worker lost its real process/flock witness before writing a terminal attempt result",
                }
                _atomic_write(result_path, result)
        if result is None:
            return current, True

        raw_after = self.github.get_pr(current.number)
        try:
            proof = self._target_landing_proof(raw_after)
        except LifecycleError as exc:
            current.state = LifecycleState.WAITING_INFRASTRUCTURE
            current.state_label = STATE_LABELS[LifecycleState.WAITING_INFRASTRUCTURE]
            current.residual_reason = f"attempt completed but target-specific landing readback is unavailable: {exc}"
            current.human_action = None
            return current, False
        if proof["landed"]:
            return (
                self._finalize_landed_attempt(
                    current,
                    raw_after,
                    result,
                    proof,
                    terminal_cleaner=terminal_cleaner,
                    notify=notify,
                ),
                False,
            )

        reread = self.inspect(raw_after)
        if reread.head != current.head and not bool(raw_after.get("merged") or raw_after.get("merged_at")):
            finalize_attempt_result(
                result,
                outcome="HEAD_CHANGED",
                retryable=False,
                reason="Integration produced a new exact head; fresh Review is required",
                target_proof=proof,
                next_owner="Review",
                next_action="perform fresh independent exact-head Review before Integration resumes",
            )
            return reread, False

        recovery = self._target_recovery_pr(current, str(proof["target_branch"]))
        if recovery is not None:
            recovery_number = int(recovery.get("number") or 0)
            recovery_head = str((recovery.get("head") or {}).get("sha") or "")
            finalize_attempt_result(
                result,
                outcome="RECOVERY_PR_CREATED",
                retryable=False,
                reason=(
                    f"mechanical target recovery PR #{recovery_number} created at {recovery_head}; "
                    "fresh exact-head Review owns the next step"
                ),
                target_proof=proof,
                next_owner="Review",
                next_action=f"review target-recovery PR #{recovery_number} at its exact current head",
            )
            reread.residual_reason = (
                f"target recovery PR #{recovery_number} is published for fresh exact-head Review; "
                "no Marco relay is required"
            )
            reread.human_action = None
            return reread, False

        exit_code = result.get("process_exit_code")
        if exit_code == 0:
            finalize_attempt_result(
                result,
                outcome="SUCCESSFUL_NOOP",
                retryable=False,
                reason=(
                    "Integration child exited 0 but neither intended-target landing, a new review head, "
                    "nor a bound target-recovery PR was authoritatively read back"
                ),
                target_proof=proof,
                next_owner="Integration / Development Workflow",
                next_action="inspect the retained attempt stdout/stderr and resolve the typed blocker before any new model attempt",
            )
            reread.residual_reason = (
                "typed Integration outcome SUCCESSFUL_NOOP: child exited 0 without authoritative intended-target "
                "landing, head movement, or recovery PR; automatic model retry is stopped"
            )
            reread.human_action = None
            return reread, False

        retryable = bool(result.get("retryable")) or bool(
            transient_infrastructure_reason(result.get("stdout_path"), result.get("stderr_path"))
        )
        if retryable and generation < MAX_INTEGRATION_ATTEMPTS:
            finalize_attempt_result(
                result,
                outcome="RETRYABLE_INFRASTRUCTURE",
                retryable=True,
                reason=(
                    f"typed transient infrastructure failure; bounded retry {generation + 1}/"
                    f"{MAX_INTEGRATION_ATTEMPTS} permitted"
                ),
                target_proof=proof,
                next_owner="Lifecycle controller",
                next_action=f"perform bounded typed retry {generation + 1}/{MAX_INTEGRATION_ATTEMPTS}",
            )
            reread.residual_reason = (
                f"typed transient Integration infrastructure failure; bounded retry "
                f"{generation + 1}/{MAX_INTEGRATION_ATTEMPTS} will run"
            )
            reread.human_action = None
            return reread, True

        outcome = "RETRY_BUDGET_EXHAUSTED" if retryable else "FAILED"
        terminal_reason = str(result.get("terminal_detail") or result.get("reason") or f"process exit {exit_code}")
        finalize_attempt_result(
            result,
            outcome=outcome,
            retryable=False,
            reason=(
                f"{terminal_reason}; retry budget exhausted at {generation}/{MAX_INTEGRATION_ATTEMPTS}"
                if retryable
                else terminal_reason
            ),
            target_proof=proof,
            next_owner="Integration / Development Workflow",
            next_action="inspect retained terminal attempt evidence and resolve the exact blocker; automatic model retry is stopped",
        )
        reread.residual_reason = (
            f"typed Integration outcome {outcome}: {terminal_reason}; no opaque model retry will occur"
        )
        reread.human_action = None
        return reread, False

    def _dispatch_local_integration(
        self,
        current,
        *,
        terminal_cleaner=None,
        notify=None,
    ):
        launcher = self.local_integration_launcher
        if launcher is None or not launcher.command or not self.integration_capable:
            current.residual_reason = (
                "local Git-capable Integration launcher is unavailable on this host; V1-A has no remote/connector/"
                "broker landing fallback"
            )
            current.human_action = None
            return current
        try:
            handoff = self._ensure_local_integration_handoff(current)
        except LifecycleError as exc:
            current.residual_reason = str(exc)
            current.human_action = None
            return current

        before_claim = self.inspect(self.github.get_pr(current.number))
        if before_claim.head != current.head:
            return before_claim
        if not (
            before_claim.state in {LifecycleState.INTEGRATION_READY, LifecycleState.MERGING}
            or self._integration_reconciliation_required(before_claim)
        ):
            return before_claim

        fence = LocalIntegrationFence(
            repository=self.github.repository,
            pr_number=before_claim.number,
            branch=before_claim.branch,
            head=before_claim.head,
            review_id=handoff["review_id"],
            task_ids=list(before_claim.task_ids),
            main_sha=handoff["main_sha"],
            target_branch=handoff["target_branch"],
            handoff_comment_id=handoff["comment_id"],
            handoff_key_value=handoff["key"],
        )
        prior = fence.recovery_state()
        if prior is not None:
            consumed, retry = self._consume_previous_attempt(
                before_claim,
                fence,
                prior,
                terminal_cleaner=terminal_cleaner,
                notify=notify,
            )
            if not retry:
                return consumed
            before_claim = consumed

        try:
            acquired = fence.acquire()
        except LifecycleError as exc:
            before_claim.residual_reason = f"local Integration claim recovery failed: {exc}"
            before_claim.human_action = None
            return before_claim
        if not acquired:
            live = fence.liveness()
            if live["running"]:
                before_claim.state = LifecycleState.MERGING
                before_claim.state_label = STATE_LABELS[LifecycleState.MERGING]
                before_claim.residual_reason = (
                    f"RUNNING — real flock + live process witness worker={live['worker_pid']} "
                    f"child={live['child_pid']}; controller reconciliation continues independently"
                )
            else:
                before_claim.state = LifecycleState.WAITING_INFRASTRUCTURE
                before_claim.state_label = STATE_LABELS[LifecycleState.WAITING_INFRASTRUCTURE]
                before_claim.residual_reason = (
                    "Integration lock is held without sufficient live process evidence; duplicate mutation refused"
                )
            before_claim.human_action = None
            return before_claim

        try:
            raw_pr = self.github.get_pr(before_claim.number)
            claim = fence.payload()
            target_branch = handoff["target_branch"]
            recovery_marker = (
                f"<!-- {TARGET_RECOVERY_MARKER} source_pr={before_claim.number} "
                f"source_head={before_claim.head} target={target_branch} -->"
            )
            context = {
                "schema": HANDOFF_SCHEMA,
                "repository": self.github.repository,
                "pull_request": {
                    "number": before_claim.number,
                    "url": before_claim.url,
                    "branch": before_claim.branch,
                    "head": before_claim.head,
                    "base": before_claim.base,
                    "body": str(raw_pr.get("body") or ""),
                },
                "target": {
                    "branch": target_branch,
                    "ref": f"refs/heads/{target_branch}",
                    "observed_sha": handoff["main_sha"],
                },
                "task_ids": list(before_claim.task_ids),
                "review": handoff["review"],
                "merge_method": self.merge_method,
                "handoff": {
                    "comment_id": handoff["comment_id"],
                    "key": handoff["key"],
                    "observed_target_sha": handoff["main_sha"],
                },
                "claim": claim,
                "lifecycle": before_claim.json(),
                "attempt_contract": {
                    "max_attempts": MAX_INTEGRATION_ATTEMPTS,
                    "result_schema": "dish-integration-attempt-result-v1",
                    "stdout_stderr_retained_on_exit_zero": True,
                },
                "instruction": (
                    "Act as the sole local Dish Integration execution for this exact PR/head. The inherited OS flock "
                    "is the single-owner mutation fence. Re-read live GitHub and the explicit owning Asana task before "
                    "first mutation and again immediately before merge. Fetch current origin and the intended target "
                    f"`refs/heads/{target_branch}`. Run literal PRE-INTEGRATION evidence. Mechanical reconciliation is "
                    "allowed only when the outcome is already determined; any semantic choice stops and routes to "
                    "Implementation. If content changes the PR head, publish the new head and STOP for fresh independent "
                    "exact-head Review. Merge only the unchanged reviewed head with expected-head/current-state "
                    "protection. Do not treat GitHub merged=true on an intermediate stacked base as source completion; "
                    "require the merge effect to reach the intended target lineage. For an already-merged intermediate "
                    "stack member, if the target is provably a prefix and this exact candidate is the ordered missing "
                    "semantic suffix, publish one cumulative recovery PR to the target, keep the same task/source "
                    f"lineage, include exact marker `{recovery_marker}` in its body, and STOP for fresh exact-head "
                    "Review. If equivalence is not mechanically provable, return one exact blocker. The worker retains "
                    "complete stdout/stderr and a typed per-attempt result regardless of process exit code."
                ),
            }
            try:
                attempt = launcher.dispatch_background(context, fence=fence)
            except LifecycleError as exc:
                fence.finish(
                    status="failed",
                    phase="returned",
                    next_action=f"local Integration worker start failed: {exc}",
                )
                before_claim.residual_reason = f"local Integration worker start failed: {exc}"
                before_claim.human_action = None
                return before_claim
            before_claim.state = LifecycleState.MERGING
            before_claim.state_label = STATE_LABELS[LifecycleState.MERGING]
            before_claim.residual_reason = (
                f"RUNNING — fenced Integration worker pid={attempt['worker_pid']} attempt "
                f"{attempt['generation']}/{MAX_INTEGRATION_ATTEMPTS}; controller reconciliation continues independently"
            )
            before_claim.human_action = None
            return before_claim
        finally:
            fence.release()

    def dispatch_one(
        self,
        pr,
        *,
        workspace,
        local_reviewer,
        implementation_fixer=None,
        terminal_cleaner=None,
        notify=None,
    ):
        current = self.inspect(self.github.get_pr(pr.number))
        if current.state == LifecycleState.LOCAL_CERTIFICATION_REQUIRED and self.integration_authority:
            result = self._dispatch_local_certification(current)
            if result is not None:
                return result
        if self.integration_authority and (
            current.state in {LifecycleState.INTEGRATION_READY, LifecycleState.MERGING}
            or self._integration_reconciliation_required(current)
        ):
            return self._dispatch_local_integration(
                current,
                terminal_cleaner=terminal_cleaner,
                notify=notify,
            )
        return super().dispatch_one(
            pr,
            workspace=workspace,
            local_reviewer=local_reviewer,
            implementation_fixer=implementation_fixer,
            terminal_cleaner=terminal_cleaner,
            notify=notify,
        )
