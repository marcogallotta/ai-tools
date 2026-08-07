"""Human-first terminal rendering for ``dish-admin`` results."""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _command_from_action(action: Mapping[str, Any]) -> str | None:
    return _clean(
        action.get("shell_command")
        or action.get("admin_command")
        or action.get("admin_command_template")
    )


def _actions(
    data: Mapping[str, Any], errors: Iterable[Mapping[str, Any]] = ()
) -> list[Mapping[str, Any]]:
    actions: list[Mapping[str, Any]] = []

    def collect(source: Mapping[str, Any]) -> None:
        rows = source.get("human_actions")
        if isinstance(rows, list):
            actions.extend(row for row in rows if isinstance(row, Mapping))
        action = source.get("human_action")
        if isinstance(action, Mapping):
            actions.append(action)
        required = source.get("required_action")
        if isinstance(required, Mapping):
            nested = required.get("human_action")
            if isinstance(nested, Mapping):
                merged = dict(nested)
                merged.setdefault(
                    "shell_command",
                    required.get("admin_command")
                    or required.get("admin_command_template"),
                )
                actions.append(merged)

    collect(data)
    for error in errors:
        if isinstance(error, Mapping):
            collect(error)

    unique: list[Mapping[str, Any]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for action in actions:
        key = (
            _clean(action.get("kind")),
            _clean(action.get("summary")),
            _command_from_action(action),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(action)
    return unique


def _success_lines(command: str, data: Mapping[str, Any]) -> list[str]:
    effect = _clean(data.get("effect") or data.get("message"))
    if effect:
        lines = [effect]
    else:
        summaries = {
            "authorize-governed-change": (
                "Authorization recorded. The task itself was not changed.",
                "Have the agent retry the same exact candidate.",
            ),
            "review-approve": (
                "The exact linked change bundle was approved.",
                "Any fresh eligible agent may now claim and apply the stored candidate.",
            ),
            "review-reject": (
                "The proposal was rejected. No task content or governed authorization changed.",
            ),
            "record-human-decision": (
                "Marco's decision was recorded and the Human Review hold was released.",
                "This did not edit or authorize governed fields.",
            ),
            "supply-evidence": (
                "The supplied evidence was recorded and the hold was released.",
            ),
            "resolved": (
                "The Verification hold was released without editing or approving the candidate.",
            ),
            "recover-lease": (
                "The expired lease was released for the original durable run.",
                "Workflow ownership was not transferred to a different run.",
            ),
            "expire-lease": (
                "The active lease was released.",
                "Workflow or Verification-cycle ownership was not transferred.",
            ),
            "abandon-operation": (
                "The dead agent attempt was recorded for abandonment.",
            ),
            "reconcile-abandonment": (
                "The abandonment was reconciled against current durable and live state.",
            ),
            "repair-destination": (
                "The recorded destination was repaired.",
            ),
            "reopen-planning": (
                "The completed task was reopened for Planning.",
            ),
            "reopen": (
                "The held Verification candidate was substantively reopened.",
            ),
            "recover": (
                "The interrupted effect was checked against the live task.",
            ),
            "discard": (
                "The provably unapplied operation was cancelled.",
            ),
            "migrate": (
                "The task schema migration completed.",
            ),
            "backup-create": (
                "The database backup was created and validated.",
            ),
            "backup-restore": (
                "The database backup was restored.",
            ),
        }
        lines = list(summaries.get(command, ("Command completed successfully.",)))

    next_step = _clean(
        data.get("next_step")
        or data.get("legal_next_step")
        or data.get("instruction")
    )
    if next_step:
        lines.append(f"Next: {next_step}")
    return lines


def render_admin_result(
    result: Mapping[str, Any], *, profile: str, verbose: bool = False
) -> str:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    lines: list[str] = [f"Environment: {str(profile or 'prod').upper()}"]

    title = _clean(data.get("task_title"))
    task_gid = _clean(result.get("task_gid") or data.get("task_gid"))
    operation_id = _clean(result.get("submission_id") or data.get("operation_id"))
    if title:
        lines.append(f"Task: {title}")
    elif task_gid:
        lines.append(f"Task: {task_gid}")
    if operation_id:
        lines.append(f"Operation: {operation_id}")
    lines.append("")

    ok = bool(result.get("ok"))
    command = _clean(result.get("command")) or "command"
    if command in {"review-queue", "review-inspect"} and ok:
        if command == "review-queue":
            items = data.get("review_items") if isinstance(data.get("review_items"), list) else data.get("proposals") if isinstance(data.get("proposals"), list) else []
            lines.append(f"Review items: {len(items)}")
            if not items:
                lines.append("No review items match this filter.")
            for index, proposal in enumerate(items, start=1):
                if not isinstance(proposal, Mapping):
                    continue
                lines.append("")
                proposal_id = _clean(proposal.get("review_id") or proposal.get("proposal_id")) or "unknown"
                status = (_clean(proposal.get("status")) or "unknown").upper()
                title = _clean(proposal.get("candidate_title")) or _clean(proposal.get("task_gid")) or "Task"
                item_type = _clean(proposal.get("item_type")) or "semantic_proposal"
                label = {
                    "semantic_proposal": "CHANGE PROPOSAL",
                    "human_review": "HUMAN DECISION",
                    "verification_hold": "VERIFICATION HOLD",
                }.get(item_type, "REVIEW")
                lines.append(f"{index}. [{status}] [{label}] {title}")
                lines.append(f"   Review ID: {proposal_id}")
                reason = _clean(proposal.get("proposal_reason"))
                if reason:
                    lines.append(f"   Why: {reason}")
                changes = proposal.get("changes") if isinstance(proposal.get("changes"), list) else []
                if changes:
                    fields = ", ".join(str(item.get("field")) for item in changes if isinstance(item, Mapping))
                    if fields:
                        lines.append(f"   Changes: {fields}")
                lines.append(f"   Inspect: dish-admin review-inspect {proposal_id}")
                if item_type == "semantic_proposal" and status == "PENDING":
                    lines.append(f"   Approve: dish-admin review-approve {proposal_id}")
                    lines.append(f"   Reject: dish-admin review-reject {proposal_id} --reason '<why>'")
                elif item_type == "human_review":
                    lines.append(
                        f"   Decide: dish-admin review-approve {proposal_id} --detail '<Marco decision and reasoning>'"
                    )
                elif item_type == "verification_hold":
                    lines.append(f"   Release: dish-admin review-approve {proposal_id}")
            if items:
                lines.append("")
                lines.append("Queue numbers are accepted only for the current queue view; UUIDs are safer to copy or share.")
        else:
            proposal = data.get("review_item") if isinstance(data.get("review_item"), Mapping) else data.get("proposal") if isinstance(data.get("proposal"), Mapping) else {}
            proposal_id = _clean(proposal.get("review_id") or proposal.get("proposal_id")) or "unknown"
            lines.append(f"Proposal {proposal_id}")
            lines.append(f"Status: {_clean(proposal.get('status')) or 'unknown'}")
            explanation = proposal.get("explanation") if isinstance(proposal.get("explanation"), Mapping) else {}
            for label, key in (("Problem", "problem"), ("Cause", "cause"), ("Why this route", "why_not_ordinary_correction"), ("Recommended", "recommended_resolution"), ("Scope", "scope"), ("After approval", "after_success")):
                value = _clean(explanation.get(key))
                if value:
                    lines.append(f"{label}: {value}")
            changes = proposal.get("changes") if isinstance(proposal.get("changes"), list) else []
            if changes:
                lines.append("")
                lines.append("Governed changes requiring Marco approval")
                for item in changes:
                    if not isinstance(item, Mapping):
                        continue
                    lines.append(f"- {item.get('field')}: {json.dumps(item.get('before'), ensure_ascii=False)} -> {json.dumps(item.get('after'), ensure_ascii=False)}")
            linked = proposal.get("linked_changes") if isinstance(proposal.get("linked_changes"), list) else []
            if linked:
                lines.append("")
                lines.append("Complete linked candidate change set")
                for item in linked:
                    if not isinstance(item, Mapping):
                        continue
                    path = item.get("path") or item.get("field") or "unknown"
                    lines.append(
                        f"- {path}: {json.dumps(item.get('before'), ensure_ascii=False)} "
                        f"-> {json.dumps(item.get('after'), ensure_ascii=False)}"
                    )
            status = _clean(proposal.get("status"))
            item_type = _clean(proposal.get("item_type")) or "semantic_proposal"
            if status == "pending" and item_type == "semantic_proposal":
                lines.append("")
                lines.append(f"Approve: dish-admin review-approve {proposal_id}")
                lines.append(f"Reject: dish-admin review-reject {proposal_id} --reason '<why>'")
            elif status == "pending" and item_type == "human_review":
                lines.append("")
                command = _clean(data.get("admin_command") or data.get("admin_command_template"))
                if command:
                    lines.append("Record decision:")
                    lines.append(command)
                lines.append(f"Or run: dish-admin review-approve {proposal_id} --detail '<Marco decision and reasoning>'")
            elif status == "pending" and item_type == "verification_hold":
                lines.append("")
                lines.append(f"Release hold: dish-admin review-approve {proposal_id}")
            elif status == "approved":
                lines.append("")
                agent_action = data.get("agent_action")
                if isinstance(agent_action, Mapping) and agent_action.get("command") == "apply-proposal":
                    lines.append(
                        f"Agent next: dish apply-proposal {proposal_id} --agent <agent> --model <model>"
                    )
                else:
                    view = data.get("authoritative_view")
                    proposal_view = (
                        view.get("semantic_proposal")
                        if isinstance(view, Mapping)
                        and isinstance(view.get("semantic_proposal"), Mapping)
                        else {}
                    )
                    block = (
                        proposal_view.get("block")
                        if isinstance(proposal_view.get("block"), Mapping)
                        else None
                    )
                    if block is not None:
                        lines.append(
                            "Agent application is currently blocked: "
                            f"{_clean(block.get('rule')) or 'authoritative state'}."
                        )
                    if operation_id:
                        lines.append(f"Refresh: dish-admin inspect {operation_id}")
    elif command == "attention" and ok:
        items = (
            data.get("attention_items")
            if isinstance(data.get("attention_items"), list)
            else []
        )
        counts = (
            data.get("category_counts")
            if isinstance(data.get("category_counts"), Mapping)
            else {}
        )
        lines.append("Dish attention")
        lines.append(f"Workflow records checked: {int(data.get('checked_count') or 0)}")
        lines.append(f"Live task inspections: {int(data.get('live_inspection_count') or 0)}")
        lines.append(f"Need attention: {int(data.get('attention_count') or 0)}")
        lines.append(
            "Safe multi-step: {multi}; Needs Marco: {marco}; Unsafe: {unsafe}".format(
                multi=int(counts.get("multi_step_safe") or 0),
                marco=int(counts.get("needs_marco") or 0),
                unsafe=int(counts.get("unsafe") or 0),
            )
        )
        if not items:
            lines.append("")
            lines.append("No abnormal workflow state needs Marco's attention.")
        category_labels = {
            "safe_cleanup": "SAFE CLEANUP",
            "multi_step_safe": "SAFE MULTI-STEP",
            "needs_marco": "NEEDS MARCO",
            "unsafe": "UNSAFE / REVIEW",
        }
        for index, item in enumerate(items, start=1):
            if not isinstance(item, Mapping):
                continue
            lines.append("")
            label = category_labels.get(
                _clean(item.get("category")) or "", "ATTENTION"
            )
            title = (
                _clean(item.get("task_title"))
                or _clean(item.get("task_gid"))
                or "Task"
            )
            lines.append(f"{index}. [{label}] {title}")
            problem = _clean(item.get("problem"))
            if problem:
                lines.append(f"   Problem: {problem}")
            operation = _clean(item.get("operation_id"))
            if operation:
                lines.append(f"   Operation: {operation}")
            item_actions = item.get("human_actions")
            if isinstance(item_actions, list):
                for action in item_actions:
                    if not isinstance(action, Mapping):
                        continue
                    summary = _clean(action.get("summary"))
                    shell = _command_from_action(action)
                    if summary:
                        lines.append(f"   Next: {summary}")
                    if shell:
                        lines.append(f"   Run: {shell}")
    elif command == "holds" and ok:
        holds = data.get("holds") if isinstance(data.get("holds"), list) else []
        lines.append(f"Open holds: {len(holds)}")
        for index, hold in enumerate(holds, start=1):
            if not isinstance(hold, Mapping):
                continue
            lines.append("")
            lines.append(
                f"{index}. {_clean(hold.get('task_title')) or _clean(hold.get('task_gid')) or 'Task'}"
            )
            question = _clean(hold.get("question"))
            if question:
                lines.append(f"   Needs: {question}")
            action = hold.get("human_action")
            if isinstance(action, Mapping):
                summary = _clean(action.get("summary"))
                shell = _command_from_action(action)
                if summary:
                    lines.append(f"   Action: {summary}")
                if shell:
                    lines.append(f"   Run: {shell}")
    elif command == "inspect" and ok:
        lines.append("Status")
        lines.append(_clean(data.get("problem")) or "No administrative blocker is recorded.")
        waiting = _clean(data.get("waiting_for"))
        if waiting:
            lines.append(f"Waiting for: {waiting}")
        operator_instruction = _clean(data.get("operator_instruction"))
        if operator_instruction:
            lines.append(f"Next: {operator_instruction}")
        lease = data.get("service_lease")
        if isinstance(lease, Mapping):
            owner = _clean(lease.get("run_id")) or "unknown"
            expiry = _clean(lease.get("expires_at"))
            lines.append(
                f"Owned by run: {owner}" + (f" until {expiry}" if expiry else "")
            )
    elif ok:
        lines.append("Done")
        lines.extend(_success_lines(command, data))
    else:
        message = _clean(data.get("message")) or "The command could not be completed."
        lines.append(f"Could not {command}")
        lines.append(message)

    actions = _actions(data, (row for row in errors if isinstance(row, Mapping)))
    if actions:
        lines.append("")
        lines.append("What you can do")
        for index, action in enumerate(actions, start=1):
            summary = (
                _clean(action.get("summary"))
                or _clean(action.get("kind"))
                or "Administrative action"
            )
            effect = _clean(action.get("effect"))
            details = action.get("details")
            shell = _command_from_action(action)
            prefix = f"{index}. " if len(actions) > 1 else ""
            lines.append(f"{prefix}{summary}")
            if isinstance(details, list):
                for detail in details:
                    clean_detail = _clean(detail)
                    if clean_detail:
                        lines.append(f"   {clean_detail}")
            if effect:
                lines.append(f"   This will: {effect}")
            if shell:
                lines.append(f"   Run: {shell}")

    agent_actions = data.get("agent_actions_now")
    if not isinstance(agent_actions, list):
        agent_actions = result.get("allowed_actions")
    if isinstance(agent_actions, list) and agent_actions:
        lines.append("")
        lines.append("Agent can now: " + ", ".join(str(item) for item in agent_actions))

    if verbose:
        lines.append("")
        lines.append("Technical details")
        lines.append(f"Code: {result.get('code')}")
        if errors:
            rules = [
                str(row.get("rule"))
                for row in errors
                if isinstance(row, Mapping) and row.get("rule")
            ]
            if rules:
                lines.append("Rules: " + ", ".join(rules))
        lines.append(json.dumps(result, indent=2, sort_keys=True, default=str))

    return "\n".join(lines).rstrip() + "\n"
