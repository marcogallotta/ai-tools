"""Action-first operator rendering for PR lifecycle state.

This is presentation only. It consumes ``PRLifecycle`` and never creates a second
lifecycle decision or role authority.
"""
from __future__ import annotations

from typing import Any

from pr_lifecycle_owner import owning_task_identity_from_references
from pr_lifecycle_host_routing import classify_local_work_item
from pr_lifecycle_support import LifecycleState, PRLifecycle


def local_work_classification(pr: PRLifecycle) -> tuple[str | None, str | None]:
    pending = [item for item in pr.local_work if item.get("required") and not item.get("completed")]
    if not pending:
        return None, None
    boundary = classify_local_work_item(pending[0])
    return boundary.work_type, boundary.scope


def _review_phrase(pr: PRLifecycle) -> str:
    if pr.review_verdict == "BLOCK":
        return "Review blocked this exact candidate"
    if pr.review_verdict == "MERGE":
        return "Review accepted this exact candidate"
    return "The exact-head Review has not finished"


def action_first_status(pr: PRLifecycle) -> str:
    task, task_error = owning_task_identity_from_references(pr.task_ids)
    if task is None:
        task = "unknown" if task_error else (pr.task_ids[0] if pr.task_ids else "unknown")
    head = pr.head[:12]
    state = pr.state

    if state == LifecycleState.MERGED:
        first = "No action for you — source integration is complete."
        why = "GitHub reports the pull request merged; any runtime or acceptance gate remains separate."
        owner = "Next owner/system: task/runtime authority if any residual gate exists."
    elif state == LifecycleState.CLOSED:
        first = "No action for you — this pull-request lineage is closed."
        why = pr.residual_reason or "GitHub reports the PR closed without an active source candidate."
        owner = "Next owner/system: owning task/orchestration authority."
    elif state == LifecycleState.REVIEW_IN_PROGRESS:
        first = "No action for you — wait for exact-head Review."
        why = "Independent Review is evaluating the current candidate."
        owner = "Next owner/system: Review."
    elif state == LifecycleState.REVIEW_READY:
        first = "No action for you — Review is next."
        why = "The authoring candidate is review-ready and any prior head review does not transfer."
        owner = "Next owner/system: Review dispatch."
    elif state == LifecycleState.CHANGES_REQUESTED:
        if pr.review_verdict == "BLOCK":
            first = "Review blocked the candidate; automatic Implementation fix is next."
            why = pr.residual_reason or "The current formal BLOCK applies to this exact head."
        else:
            first = "No action for you — failed CI ownership must be resolved before any source fix."
            why = pr.residual_reason or "Semantic mutation is allowed only for a proven PR-owned failure."
        owner = "Next owner/system: mutation broker and Implementation/fix if eligible."
    elif state == LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
        first = "No action for you — Implementation continuation is next."
        why = pr.residual_reason or "The draft PR still has unfinished authoring evidence."
        owner = "Next owner/system: mutation broker and Implementation."
    elif state == LifecycleState.AUTHORING:
        first = "No action for you — Implementation is still authoring the candidate."
        why = pr.residual_reason or "The PR remains draft/authoring state."
        owner = "Next owner/system: Implementation."
    elif state in {LifecycleState.REVIEW_PASSED, LifecycleState.WAITING_CI}:
        first = "No action for you — Review accepted the candidate; wait for exact-head certification/Integration gates."
        why = pr.residual_reason or "The semantic Review passed, but a later landing gate is still pending."
        owner = "Next owner/system: Integration/gate evaluation."
    elif state == LifecycleState.WAITING_EXTERNAL_DEPENDENCY:
        first = "No action for you — wait for the named external dependency owner."
        why = pr.residual_reason or "A durable external dependency blocks the current candidate."
        owner = "Next owner/system: external dependency owner, then lifecycle re-evaluation."
    elif state in {LifecycleState.LOCAL_CERTIFICATION_REQUIRED, LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED}:
        kind, scope = local_work_classification(pr)
        first = "Your next action is to send the exact PR head to the required local work path."
        why = f"LOCAL WORK TYPE: {kind}. LOCAL SCOPE: {scope}."
        owner = "Next owner/system: local tests." if kind == "TESTS ONLY" else "Next owner/system: local Implementation/publication."
    elif state == LifecycleState.INTEGRATION_READY:
        first = "No action for you — Review accepted the candidate; Integration is next."
        why = pr.residual_reason or "All current exact-head landing gates are green."
        owner = "Next owner/system: the configured local Claude/Codex Integration launcher."
    elif state == LifecycleState.MERGING:
        first = "No action for you — authorized Integration is in progress."
        why = pr.residual_reason or "The exact reviewed head is inside the landing boundary."
        owner = "Next owner/system: Integration."
    else:  # pragma: no cover - enum exhaustiveness guard
        first = "No action for you — wait for lifecycle re-evaluation."
        why = pr.residual_reason or pr.state_label
        owner = "Next owner/system: PR lifecycle."

    if pr.human_action:
        lowered = pr.human_action.lower()
        if "no action for marco" not in lowered and not lowered.startswith("waiting on "):
            # A durable real operator obligation overrides a generic automatic/wait
            # headline.  The lifecycle remains the source of truth; this only makes the
            # human action explicit first.
            first = f"Your next action: {pr.human_action}"
    review = _review_phrase(pr)
    return (
        f"{first} {why} Task {task}: {pr.state_label}. "
        f"PR #{pr.number} @ {head}: {review}. {owner}"
    )
