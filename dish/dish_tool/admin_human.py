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
    if command == "holds" and ok:
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
            shell = _command_from_action(action)
            prefix = f"{index}. " if len(actions) > 1 else ""
            lines.append(f"{prefix}{summary}")
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
