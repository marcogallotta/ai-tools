"""Action-first operator rendering for PR lifecycle state.

This is presentation only. It consumes ``PRLifecycle`` and never creates a second
lifecycle decision or role authority.
"""
from __future__ import annotations

from collections.abc import Mapping

from pr_lifecycle_host_routing import classify_local_work_item
from pr_lifecycle_support import LifecycleState, PRLifecycle


def local_work_classification(pr: PRLifecycle) -> tuple[str | None, str | None]:
    pending = [item for item in pr.local_work if item.get("required") and not item.get("completed")]
    if not pending:
        return None, None
    boundary = classify_local_work_item(pending[0])
    return boundary.work_type, boundary.scope


def _operator_action(pr: PRLifecycle) -> str:
    if not pr.human_action:
        return "NONE"
    lowered = pr.human_action.lower()
    if "no action for marco" in lowered or lowered.startswith("waiting on "):
        return "NONE"
    return pr.human_action


def _rollout_readiness(pr: PRLifecycle) -> tuple[str, str, str]:
    """Return ACTIVE/RUNNING, STATUS, COMPLETION PROOF from authoritative rollout projection."""
    if not pr.task_ids:
        return (
            "UNKNOWN — no linked task exists to prove whether activation is required",
            "ACTIVATION PENDING",
            "source is landed, but activation requirement/readback is unavailable because no owning task is linked",
        )

    by_gid = {
        str(item.get("gid") or ""): item
        for item in pr.asana
        if isinstance(item, Mapping)
    }
    tasks = [by_gid.get(str(gid)) for gid in pr.task_ids]
    if any(
        task is None
        or task.get("error")
        or task.get("rollout_error")
        or "rollout" not in task
        for task in tasks
    ):
        missing = next(
            (
                str(gid)
                for gid, task in zip(pr.task_ids, tasks)
                if task is None or task.get("error") or task.get("rollout_error") or "rollout" not in task
            ),
            "unknown",
        )
        return (
            f"UNKNOWN — authoritative rollout reconstruction is unavailable for task {missing}",
            "ACTIVATION PENDING",
            f"source is landed, but task {missing} lacks authoritative rollout requirement/readback evidence",
        )

    no_rollout_pending: list[str] = []
    for gid, task in zip(pr.task_ids, tasks):
        if not isinstance(task, Mapping) or task.get("rollout") is not None:
            continue
        requirement = str(task.get("activation_requirement") or "unknown")
        evidence = str(task.get("activation_requirement_evidence") or "").strip()
        if requirement != "not-required":
            detail = evidence or "no explicit exact-head no-activation declaration exists"
            no_rollout_pending.append(f"task {gid}: {detail}; no rollout plan/readback exists yet")
    if no_rollout_pending:
        proof = "; ".join(no_rollout_pending)
        return (
            "UNKNOWN — required activation/readback is not yet proven",
            "ACTIVATION PENDING",
            f"source is landed, but {proof}",
        )

    rollouts = [task.get("rollout") for task in tasks if isinstance(task, Mapping) and task.get("rollout") is not None]
    if not rollouts:
        task_text = ", ".join(str(gid) for gid in pr.task_ids)
        evidence = "; ".join(
            str(task.get("activation_requirement_evidence") or "").strip()
            for task in tasks
            if isinstance(task, Mapping) and task.get("activation_requirement_evidence")
        )
        return (
            "NOT REQUIRED — exact-head Review explicitly declares no post-merge activation gate",
            "OPERATIONAL",
            f"target-specific source landing passed for task(s) {task_text}; {evidence}",
        )

    descriptors: list[str] = []
    statuses: list[str] = []
    for rollout in rollouts:
        if not isinstance(rollout, Mapping):
            return (
                "UNKNOWN — rollout projection is malformed",
                "ACTIVATION PENDING",
                "source is landed, but the authoritative rollout projection is malformed",
            )
        plan_id = str(rollout.get("plan_id") or "unknown")
        generation = rollout.get("generation")
        stages = [item for item in rollout.get("stages") or [] if isinstance(item, Mapping)]
        if not stages:
            return (
                f"UNKNOWN — rollout {plan_id} generation {generation} has no stage projection",
                "ACTIVATION PENDING",
                "source is landed, but rollout stage evidence is unavailable",
            )
        final = stages[-1]
        final_state = str(final.get("state") or "PENDING").upper()
        activated = [item for item in stages if str(item.get("state") or "").upper() == "ACTIVATED"]
        rejected = [item for item in stages if str(item.get("state") or "").upper() == "REJECTED"]
        if rejected:
            item = rejected[-1]
            statuses.append("NOT OPERATIONAL")
            descriptors.append(
                f"rollout {plan_id} generation {generation} stage {item.get('stage')} REJECTED "
                f"for artifact {item.get('artifact')} config {item.get('config')}"
            )
        elif bool(rollout.get("complete")) and final_state == "ACCEPTED":
            statuses.append("OPERATIONAL")
            descriptors.append(
                f"rollout {plan_id} generation {generation} final stage {final.get('stage')} ACCEPTED "
                f"for artifact {final.get('artifact')} config {final.get('config')} "
                f"activated identity {final.get('activated_identity')}"
            )
        elif final_state == "CANCELLED":
            statuses.append("NOT OPERATIONAL")
            descriptors.append(
                f"rollout {plan_id} generation {generation} final stage {final.get('stage')} CANCELLED"
            )
        elif activated:
            item = activated[-1]
            statuses.append("VERIFYING")
            descriptors.append(
                f"rollout {plan_id} generation {generation} stage {item.get('stage')} ACTIVATED "
                f"for artifact {item.get('artifact')} config {item.get('config')} "
                f"activated identity {item.get('activated_identity')}"
            )
        else:
            statuses.append("ACTIVATION PENDING")
            descriptors.append(
                f"rollout {plan_id} generation {generation} final stage {final.get('stage')} is {final_state}"
            )

    if "NOT OPERATIONAL" in statuses:
        status = "NOT OPERATIONAL"
    elif "ACTIVATION PENDING" in statuses:
        status = "ACTIVATION PENDING"
    elif "VERIFYING" in statuses:
        status = "VERIFYING"
    else:
        status = "OPERATIONAL"

    proof = "; ".join(descriptors)
    if status == "OPERATIONAL":
        active = "PROVEN — " + proof
    elif status == "VERIFYING":
        active = "RUNNING/ACTIVE — " + proof
    elif status == "NOT OPERATIONAL":
        active = "NOT OPERATIONAL — " + proof
    else:
        active = "NOT YET PROVEN — " + proof
    return active, status, proof


def _readiness_fields(pr: PRLifecycle) -> str:
    state = pr.state
    action = _operator_action(pr)

    if state == LifecycleState.MERGED:
        source = f"LANDED — exact reviewed source `{pr.head}`; target-specific GitHub readback passed"
        active, status, activation_proof = _rollout_readiness(pr)
        proof = (
            f"{pr.residual_reason}; {activation_proof}"
            if pr.residual_reason
            else activation_proof
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
        _active, merged_status, _proof = _rollout_readiness(pr)
        if merged_status == "OPERATIONAL":
            first = "Source integration is complete and the required activation boundary is satisfied. Nothing for you to do."
            why = "The lifecycle has authoritative source and activation/no-activation evidence."
        elif merged_status == "VERIFYING":
            first = "Source integration is complete and activation is being verified. Nothing for you to do."
            why = "The current rollout generation is activated but not yet terminally accepted."
        elif merged_status == "NOT OPERATIONAL":
            first = "Source integration is complete, but the rollout is not operational. Nothing for you to do unless an exact action is named below."
            why = "The authoritative rollout state is rejected or cancelled."
        else:
            first = "Source integration is complete. Nothing for you to do."
            why = "A required activation/readback step remains separate."
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

    action = _operator_action(pr)
    if action != "NONE":
        first = f"Your next action: {action}"
    return f"{first} {why} {_readiness_fields(pr)}"
