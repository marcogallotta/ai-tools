"""Local-only Integration dispatch for certification, reconciliation, and landing."""
from __future__ import annotations

from typing import Any

import pr_gate
from pr_lifecycle_local_integration import (
    HANDOFF_SCHEMA,
    LocalIntegrationFence,
    find_handoff,
    handoff_key,
    marker,
)
from pr_lifecycle_support import LifecycleError, LifecycleState
from pr_lifecycle_helpers import local_work_from_review


class LocalIntegrationCertificationMixin:
    """Dispatch exact-head Integration work only to the configured local launcher."""

    local_integration_launcher: Any | None = None

    @staticmethod
    def _integration_reconciliation_required(current) -> bool:
        if current.state != LifecycleState.REVIEW_PASSED:
            return False
        residual = str(current.residual_reason or "").lower()
        return any(token in residual for token in ("mergeab", "integration ordering", "base", "conflict"))

    @staticmethod
    def _review_id(exact_review: dict[str, Any] | None) -> int | None:
        if exact_review is None:
            return None
        try:
            value = int(exact_review.get("id"))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _ensure_local_integration_handoff(self, current) -> dict[str, Any]:
        reviews = self.github.get_reviews(current.number)
        exact_review = pr_gate.latest_exact_head_review(reviews, reviewed_head=current.head)
        review_id = self._review_id(exact_review)
        if exact_review is None or review_id is None or str(exact_review.get("verdict")) != "MERGE":
            raise LifecycleError("local Integration handoff requires one authoritative exact-head MERGE review")
        main_sha = self.github.get_ref_sha(f"heads/{current.base}")
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
            body = (
                f"{handoff_marker}\n"
                f"LOCAL INTEGRATION V1-A HANDOFF — exact reviewed head `{current.head}`\n\n"
                f"Mode: {mode}\n"
                f"PR: #{current.number} `{current.branch}` -> `{current.base}`\n"
                f"Owning Asana task(s): {task_line}\n"
                f"Exact Review: `{review_id}` verdict `MERGE`\n"
                f"Observed target `{current.base}` at handoff creation: `{main_sha}`\n\n"
                "This is a local-only Integration handoff. Final Integration/merge must run on a local "
                "Claude/Codex host with a live checkout and real Git/worktree tooling. There is no ChatGPT, "
                "connector or GitHub Actions landing fallback.\n\n"
                "Before the first mutation and again immediately before the irreversible merge boundary, re-read "
                "the live GitHub PR/head/base/Review and the explicit owning Asana task. Fetch current origin state. "
                "Use expected-head protection for publication/merge and require authoritative GitHub MERGED readback.\n\n"
                "Only conflict-free/mechanical reconciliation already determined by current authority is allowed. "
                "A semantic/product/schema/policy/test-weakening choice stops and returns to Implementation. Any "
                "content-changing reconciliation creates a new PR head and must stop for fresh independent exact-head "
                "Review before landing.\n\n"
                "The launcher receives a repository-owned per-PR/head claim. The OS lock is consequential-mutation "
                "admission; the JSON checkpoint is crash/compaction recovery state. Checkpoint certifying/reconciling/"
                "reconciled/premerge/head-changed/failed-evidence/merged with `scripts/pr_lifecycle.py integration-checkpoint`.\n\n"
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

        # Handoff creation is not mutation admission. Re-read exact PR/head/state before
        # attempting the local per-PR/head single-owner fence.
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
            handoff_comment_id=handoff["comment_id"],
            handoff_key_value=handoff["key"],
        )
        try:
            acquired = fence.acquire()
        except LifecycleError as exc:
            before_claim.residual_reason = f"local Integration claim recovery failed: {exc}"
            before_claim.human_action = None
            return before_claim
        if not acquired:
            before_claim.residual_reason = (
                "another local Integration execution already owns consequential mutation for this exact PR/head"
            )
            before_claim.human_action = None
            return before_claim

        try:
            # No advisory lease is required for synchronous V1-A execution. The OS fence
            # above is the sole local consequential-mutation admission invariant.
            raw_pr = self.github.get_pr(before_claim.number)
            claim = fence.payload()
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
                "task_ids": list(before_claim.task_ids),
                "review": handoff["review"],
                "merge_method": self.merge_method,
                "handoff": {
                    "comment_id": handoff["comment_id"],
                    "key": handoff["key"],
                    "observed_main_sha": handoff["main_sha"],
                },
                "claim": claim,
                "lifecycle": before_claim.json(),
                "instruction": (
                    "Act as the sole local Dish Integration execution for this exact PR/head. This claim's OS lock is "
                    "the single-owner mutation fence and remains held by the parent while you run; checkpoint durable "
                    "recovery state using `scripts/pr_lifecycle.py integration-checkpoint --claim-path <state_path> "
                    "--claim-id <claim_id> --phase <phase> ...`. Re-read live GitHub and the explicit owning Asana "
                    "task before first mutation and again immediately before merge. Fetch current origin/main and use "
                    "a clean Integration worktree when reconciliation/evidence needs it. Run literal PRE-INTEGRATION "
                    "evidence. Mechanical reconciliation is allowed only when the outcome is already determined; any "
                    "semantic choice stops and routes to Implementation. If content changes the PR head, publish the "
                    "new head and STOP for fresh independent exact-head Review. Merge only the unchanged reviewed head "
                    "with expected-head/current-state protection. Require authoritative GitHub MERGED readback before "
                    "returning success. There is no remote/connector/broker merge fallback."
                ),
            }
            try:
                launcher.dispatch(context, lock_fd=fence.lock_fd())
            except LifecycleError as exc:
                fence.finish(
                    status="failed",
                    phase="returned",
                    next_action=f"local Integration launcher failed: {exc}",
                )
                before_claim.residual_reason = f"local Integration launcher failed: {exc}"
                before_claim.human_action = None
                return before_claim

            raw_after = self.github.get_pr(before_claim.number)
            reread = self.inspect(raw_after)
            if reread.head != before_claim.head and reread.state != LifecycleState.MERGED:
                fence.finish(
                    status="released",
                    phase="head-changed",
                    next_action="fresh independent exact-head Review required before Integration may resume",
                    current_head=reread.head,
                )
                return reread
            if reread.state == LifecycleState.MERGED:
                merge_sha = str(raw_after.get("merge_commit_sha") or "").lower()
                result = self._finalize_authoritative_merge(
                    before_claim,
                    raw_after_merge=raw_after,
                    merge_sha=merge_sha,
                )
                if result.state == LifecycleState.MERGED:
                    fence.finish(
                        status="complete",
                        phase="merged",
                        next_action="authoritative merge readback complete; post-merge reconciliation/cleanup may continue",
                        current_head=before_claim.head,
                        merge_sha=merge_sha or None,
                    )
                    if notify is not None:
                        from pr_lifecycle_operator import action_first_status
                        notify(action_first_status(result))
                    if terminal_cleaner is not None:
                        return self._terminal_cleanup(
                            result,
                            disposition="merged",
                            terminal_cleaner=terminal_cleaner,
                            notify=notify or (lambda _: None),
                        )
                return result

            fence.finish(
                status="released",
                phase="returned",
                next_action="re-read live lifecycle and resume only if exact reviewed head remains Integration-ready",
                current_head=reread.head,
            )
            if reread.state in {LifecycleState.INTEGRATION_READY, LifecycleState.MERGING} or self._integration_reconciliation_required(reread):
                reread.residual_reason = (
                    "local Integration execution returned without authoritative MERGED readback or a new review head"
                )
                reread.human_action = None
            return reread
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
