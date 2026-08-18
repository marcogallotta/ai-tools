"""Action-first operator rendering for PR lifecycle state.

This is presentation only. It consumes ``PRLifecycle`` and never creates a second
lifecycle decision or role authority.
"""
from __future__ import annotations

from pr_lifecycle_host_routing import classify_local_work_item
from pr_lifecycle_support import LifecycleState, PRLifecycle


def local_work_classification(pr: PRLifecycle) -> tuple[str | None, str | None]:
    pending = [item for item in pr.local_work if item.get("required") and not item.get("completed")]
    if not pending:
        return None, None
    boundary = classify_local_work_item(pending[0])
    return boundary.work_type, boundary.scope


def _readiness_fields(pr: PRLifecycle) -> str:
    state = pr.state
    action = "NONE"
    if pr.human_action:
        lowered = pr.human_action.lower()
        if "no action for marco" not in lowered and not lowered.startswith("waiting on "):
            action = pr.human_action

    if state == LifecycleState.MERGED:
        source = f"LANDED — exact reviewed source `{pr.head}`; target-specific GitHub readback passed"
        active = "UNKNOWN — runtime/deployed/process generation is a separate witness"
        status = "ACTIVATION PENDING"
        proof = (
            pr.residual_reason
            if pr.residual_reason
            else f"target-specific source landing readback for `{pr.head}`; runtime activation proof still separate"
        )
    elif state == LifecycleState.MERGING:
        source = f"NOT LANDED — exact candidate `{pr.head}`"
        residual = str(pr.residual_reason or "")
        if residual.startswith("RUNNING —"):
            active = residual
        else:
            active = "UNKNOWN — no current real lock/process witness is rendered"
        status = "VERIFYING"
        proof = "authoritative intended-target landing readback is still pending"
    elif state in {LifecycleState.INTEGRATION_READY, LifecycleState.REVIEW_PASSED, LifecycleState.WAITING_CI}:
        source = f"NOT LANDED — exact candidate `{pr.head}`"
        active = "NOT RUNNING — source landing has not completed"
        status = "FIX NOT LIVE"
        proof = pr.residual_reason or "review/gate evidence exists; intended-target source landing is still pending"
    elif state == LifecycleState.WAITING_INFRASTRUCTURE:
        source = f"UNKNOWN — exact candidate `{pr.head}` cannot advance while infrastructure readback is unavailable"
        active = "UNKNOWN — missing infrastructure/liveness witness is named in the lifecycle reason"
        status = "NOT OPERATIONAL"
        proof = pr.residual_reason or "infrastructure evidence missing"
    else:
        source = f"NOT LANDED — exact candidate `{pr.head}`"
        active = "NOT RUNNING — no source-landing runtime is implied by this lifecycle state"
        status = "FIX NOT LIVE"
        proof = pr.residual_reason or f"lifecycle state `{pr.state_label}`"

    return (
        f"SOURCE: {source} | ACTIVE/RUNNING: {active} | STATUS: {status} | "
        f"OPERATOR ACTION: {action} | COMPLETION PROOF: {proof}"
    )


def action_first_status(pr: PRLifecycle) -> str:
    state = pr.state

    if state == LifecycleState.MERGED:
        first = "Source integration is complete. Nothing for you to do."
        why = "Runtime or acceptance work, if any, remains separate."
    elif state == LifecycleState.CLOSED:
        first = "This pull request is closed. Nothing for you to do."
        why = pr.residual_reason or "There is no active source candidate on this lineage."
    elif state == LifecycleState.REVIEW_IN_PROGRESS:
        first = "Review is in progress. Nothing for you to do."
        why = "The current candidate is being checked independently."
    elif state == LifecycleState.REVIEW_READY:
        first = "This is ready for review. Nothing for you to do."
        why = "The implementation candidate is complete enough for independent review."
    elif state == LifecycleState.CHANGES_REQUESTED:
        if pr.review_verdict == "BLOCK":
            first = "Review found a code problem. A fix is next. Nothing for you to do."
            why = "The current candidate needs another implementation pass before review can pass."
        else:
            first = "Automated tests failed, but it is not yet clear whether this version caused it. Nothing for you to do while that is checked."
            why = "The source must not be changed until failure ownership is known."
    elif state == LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
        first = "The implementation still has unfinished work. Nothing for you to do."
        why = "The same implementation path should finish it before review."
    elif state == LifecycleState.AUTHORING:
        first = "Implementation is still in progress. Nothing for you to do."
        why = "The candidate is still being authored."
    elif state in {LifecycleState.REVIEW_PASSED, LifecycleState.WAITING_CI}:
        first = "Review passed. Automated landing checks are still pending. Nothing for you to do."
        why = "Source integration waits for the remaining required checks."
    elif state == LifecycleState.WAITING_EXTERNAL_DEPENDENCY:
        first = "This is waiting on an external dependency. Nothing for you to do."
        why = pr.residual_reason or "The dependency owner must finish before this can continue."
    elif state == LifecycleState.WAITING_INFRASTRUCTURE:
        first = "Integration infrastructure evidence is unavailable. Nothing for you to do."
        why = pr.residual_reason or "The lifecycle controller will retry bounded infrastructure readback."
    elif state in {LifecycleState.LOCAL_CERTIFICATION_REQUIRED, LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED}:
        kind, _scope = local_work_classification(pr)
        if kind == "TESTS ONLY":
            first = f"This version needs a test on the local machine. Give PR #{pr.number} to a local test runner; the details are already on the PR."
        elif kind == "IMPLEMENTATION / PUBLICATION":
            first = f"This needs a code or publication fix on the local machine. Give PR #{pr.number} to a local coding agent; the instructions are already on the PR."
        else:
            first = f"This needs local system access. Give PR #{pr.number} to the local agent named in the PR handoff."
        why = "The repository has already recorded the complete handoff."
    elif state == LifecycleState.INTEGRATION_READY:
        first = "Review and required checks passed. Source integration is next. Nothing for you to do."
        why = "The authorized Integration path can continue automatically."
    elif state == LifecycleState.MERGING:
        first = "Source integration is in progress. Nothing for you to do."
        why = "The approved candidate is inside the landing step."
    else:  # pragma: no cover - enum exhaustiveness guard
        first = "The pull request is being re-evaluated. Nothing for you to do."
        why = "The durable lifecycle state will determine the next step."

    if pr.human_action:
        lowered = pr.human_action.lower()
        if "no action for marco" not in lowered and not lowered.startswith("waiting on "):
            first = f"Your next action: {pr.human_action}"
    return f"{first} {why} {_readiness_fields(pr)}"
