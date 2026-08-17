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
        owner = "Next owner/system: the configured local Claude/Codex Integration launcher."
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
    return f"{first} {why}"
