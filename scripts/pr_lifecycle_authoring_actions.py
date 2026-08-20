"""Draft-authoring continuation actions layered over the ordinary lifecycle dispatcher."""
from __future__ import annotations

from pr_lifecycle_support import *
from pr_lifecycle_helpers import _continuation_handoff_present, _continuation_key
from pr_lifecycle_post_merge_actions import PostMergeReviewActionsMixin
from pr_lifecycle_engine_actions import _dispatch_fixer, _fixer_command
from pr_lifecycle_host_routing import CHATGPT_IMPLEMENTATION, LOCAL_IMPLEMENTATION
from pr_lifecycle_publication_completion import finalize_same_pr_for_review
from installed_host_cert import (
    EVIDENCE as INSTALLED_HOST_CERT_EVIDENCE,
    MARKER as INSTALLED_HOST_CERT_MARKER,
    requirement_for_files,
)


class LifecycleAuthoringActionsMixin(PostMergeReviewActionsMixin):
    def finalize_implementation_pr(
        self,
        number: int,
        *,
        expected_head: str,
        clear_publication_blocker: bool = False,
        keep_draft_reason: str | None = None,
    ) -> dict[str, Any]:
        return finalize_same_pr_for_review(
            self.github,
            number=number,
            expected_head=expected_head,
            clear_publication_blocker=clear_publication_blocker,
            keep_draft_reason=keep_draft_reason,
        )

    def _ensure_implementation_continuation_handoff(
        self, pr: PRLifecycle, evidence: str, *, implementation_host: str, host_requirement=None
    ) -> bool:
        comments = self.github.get_comments(pr.number)
        if _continuation_handoff_present(comments, head=pr.head, evidence=evidence):
            return False
        key = _continuation_key(pr.head, evidence)
        marker = f"<!-- {IMPLEMENTATION_CONTINUATION_MARKER} head={pr.head} key={key} -->"
        if implementation_host == LOCAL_IMPLEMENTATION and host_requirement is not None:
            detail = (
                f"Required installed hosts: {', '.join(host_requirement.hosts)}.\n"
                f"Changed host-boundary paths: {', '.join(host_requirement.paths)}.\n\n"
                "This is pre-Review LOCAL IMPLEMENTATION continuation, not post-Review certification. "
                "The local worker owns exact-head candidate activation/fencing, actual installed-loader/tool execution, "
                "diagnosis, source fixes, retest, restoration/final-activation readback, and durable exact-head host evidence. "
                "Use mechanically verified launch identity plus the exact tools/agent-worktree task/branch/PR/head lineage; "
                "missing identity or moved lineage performs zero semantic mutation. "
            )
        else:
            detail = (
                "This is the explicit continuation ownership handoff if the prior Implementation owner is unavailable. "
            )
        self.github.add_comment(
            pr.number,
            f"{marker}\nIMPLEMENTATION CONTINUATION HANDOFF — exact head `{pr.head}`\n\n"
            f"Continue the existing Implementation branch/task and finish: {evidence}.\n\n"
            + detail
            + "Keep this PR draft until authoring evidence is complete; do not route unfinished authoring work to Review or Integration.\n\n"
            "— Dish PR lifecycle dispatcher",
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
        get_pr_files = getattr(self.github, "get_pr_files", None)
        host_requirement = requirement_for_files(get_pr_files(current.number)) if callable(get_pr_files) else None
        implementation_host = (
            LOCAL_IMPLEMENTATION
            if evidence == INSTALLED_HOST_CERT_EVIDENCE and host_requirement is not None
            else CHATGPT_IMPLEMENTATION
        )

        if any(
            lease.get("phase") in {"implementation", "fix"} for lease in current.active_leases
        ):
            return current

        selected_command = None if implementation_fixer is None else _fixer_command(implementation_fixer, implementation_host)
        if selected_command is None:
            current.residual_reason = (
                f"selected {implementation_host} continuation consumer is unavailable; unfinished evidence: {evidence}"
            )
            if implementation_host == LOCAL_IMPLEMENTATION:
                current.human_action = None
                return current
            current.human_action = f"PR #{current.number} still needs Implementation to finish {evidence}."
            self._notify_once(
                current,
                kind="implementation-continuation",
                action=evidence,
                message=current.human_action,
                notify=notify,
            )
            return current

        self._ensure_implementation_continuation_handoff(
            current, evidence, implementation_host=implementation_host, host_requirement=host_requirement
        )
        reread = self.inspect(self.github.get_pr(current.number))
        if reread.head != current.head or reread.state != LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
            return reread

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

        if implementation_host == LOCAL_IMPLEMENTATION:
            refreshed_requirement = requirement_for_files(get_pr_files(current.number)) if callable(get_pr_files) else None
            if refreshed_requirement != host_requirement:
                if lease_id is not None:
                    self._release_lease(
                        current.number, lease_id, reason="host-bound changed surface moved before local continuation dispatch"
                    )
                moved = self.inspect(self.github.get_pr(current.number))
                moved.residual_reason = "installed-host changed-surface classification changed before local dispatch"
                moved.human_action = None
                return moved

        context = {
            "schema": "dish-pr-implementation-continuation-v1",
            "repository": self.github.repository,
            "pr_url": current.url,
            "pr_number": current.number,
            "branch": current.branch,
            "head": current.head,
            "implementation_host": implementation_host,
            "task_ids": current.task_ids,
            "unfinished_authoring_evidence": evidence,
            "installed_host_certification": (
                None
                if implementation_host != LOCAL_IMPLEMENTATION or host_requirement is None
                else {
                    "required": True,
                    "evidence": INSTALLED_HOST_CERT_EVIDENCE,
                    "marker": INSTALLED_HOST_CERT_MARKER,
                    "required_hosts": list(host_requirement.hosts),
                    "changed_paths": list(host_requirement.paths),
                    "identity": {
                        "source": "mechanically verified launch provenance",
                        "claim": "tools/agent-worktree claim --require-launch-provenance",
                        "missing_or_ambiguous": "fail before activation",
                    },
                    "lineage_rereads": [
                        "before local launch",
                        "after every candidate head movement",
                        "immediately before publication",
                    ],
                    "host_window": (
                        "capture installed versions/effective config/symlinks and pre-state; fence the full interval from "
                        "final pre-mutation reread through exact-candidate activation, real host smoke, source-fix/retest, "
                        "and restoration or separately authorized final-activation readback"
                    ),
                    "acceptance": [
                        "valid explicit launch provenance establishes only the assigned identity; missing provenance stays unresolved",
                        "post-compaction re-ground recovers on the next governed action without self-lock",
                        "broken Asana/tool prerequisites are repaired boundedly before guard dependence or activation fails",
                        "shell-config literals/command substitution/malformed/noisy/timeout inputs cannot manufacture identity",
                        "installed Claude/Codex versions, effective config sources, symlink targets and digests match the candidate",
                        "actual installed loader/tool execution passes a harmless governed action and a deliberate conflict remains denied",
                        "no effective config retains a removed/disabled hook reference and the remaining chain loads",
                        "any activation that exercises changed security meaning proves the required consequential decision already exists",
                        "temporary certification restores exact prior state; final activation requires separate authorization and readback",
                        "any head movement invalidates prior host evidence",
                    ],
                }
            ),
            "lifecycle": claimed.json(),
            "instruction": (
                "Follow the current repository Implementation contract. Continue the existing draft PR, "
                "branch, and owning task; finish only the named authoring evidence, update the durable PR "
                "evidence/head, and mark ready for review only when authoring is complete. Pending ordinary CI "
                "belongs to Integration and is not authoring evidence. "
                + (
                    "For this hook/config/install-wiring continuation, LOCAL IMPLEMENTATION owns routine real-host "
                    "setup/test/diagnosis/source-fix/retest; Marco is not the tester or installer. Re-read exact live "
                    "task/PR/branch/head before launch, after head movement, and before publication; use claim-time "
                    "launch provenance, actual installed host execution, full-window host fencing, stale-reference "
                    "removal, consequential-security-decision proof where required, and restoration/final-activation readback. Post the structured exact-head installed-host "
                    "certificate only after every required check passes; then make the unchanged exact candidate Review-ready."
                    if implementation_host == LOCAL_IMPLEMENTATION
                    else ""
                )
            ),
        }
        try:
            _dispatch_fixer(implementation_fixer, context, host=implementation_host)
        except LifecycleError:
            if lease_id is not None:
                self._release_lease(
                    current.number,
                    lease_id,
                    reason="Implementation continuation dispatcher failed",
                )
            raise
        return self.inspect(self.github.get_pr(current.number))
