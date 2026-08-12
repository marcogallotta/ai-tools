"""Human-first terminal rendering for ``dish-admin`` results."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Iterable, Mapping


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _localize(value: datetime) -> datetime:
    return value.astimezone()


def _lease_began(value: Any) -> str | None:
    text = _clean(value)
    if text is None:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        started_at = datetime.fromisoformat(text)
    except ValueError:
        return None
    if started_at.tzinfo is None:
        return None

    elapsed_seconds = max(
        0,
        int(
            (
                _utc_now() - started_at.astimezone(timezone.utc)
            ).total_seconds()
        ),
    )
    if elapsed_seconds < 60:
        age = f"{elapsed_seconds}s"
    elif elapsed_seconds < 3600:
        age = f"{elapsed_seconds // 60}m"
    elif elapsed_seconds < 86400:
        hours, minutes = divmod(elapsed_seconds // 60, 60)
        age = f"{hours}h {minutes}m" if minutes else f"{hours}h"
    else:
        days, hours = divmod(elapsed_seconds // 3600, 24)
        age = f"{days}d {hours}h" if hours else f"{days}d"

    local_started = _localize(started_at)
    absolute = local_started.strftime("%Y-%m-%d %H:%M:%S")
    zone = local_started.tzname()
    if zone:
        absolute += f" {zone}"
    return f"{absolute} ({age} ago)"


def _compact_value(value: Any, *, limit: int = 110) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def _command_from_action(action: Mapping[str, Any]) -> str | None:
    return _clean(
        action.get("shell_command")
        or action.get("admin_command")
        or action.get("admin_command_template")
    )


def _command_label(action: Mapping[str, Any]) -> str:
    requires_input = action.get("requires_input")
    return "Template" if isinstance(requires_input, list) and requires_input else "Run"


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


def _decision_first_abandonment_action(
    actions: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], str] | None:
    for action in actions:
        kind = _clean(action.get("kind"))
        if kind == "abandon-dead-verifier":
            return action, "Is the previous verifier conversation permanently unavailable?"
        if kind == "abandon-dead-agent":
            return action, "Is the previous agent conversation permanently unavailable?"
    return None


def _compact_recovery_action(
    actions: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    recovery_kinds = {
        "reconcile-uncertain-effect",
        "reconcile-before-ownership-transfer",
        "recover-expired-lease",
    }
    for action in actions:
        if _clean(action.get("kind")) in recovery_kinds:
            return action
    return None


def _agent_command_name(item: Any) -> str | None:
    if isinstance(item, Mapping):
        return _clean(item.get("command"))
    return _clean(item)


def _agent_handoff_line(
    *,
    result: Mapping[str, Any],
    data: Mapping[str, Any],
    commands: Iterable[str | None],
) -> str | None:
    command_set = {command for command in commands if command}
    if not command_set:
        return None
    title = _clean(data.get("task_title"))
    task_gid = _clean(result.get("task_gid") or data.get("task_gid"))
    dish_id = _clean(data.get("dish_id"))
    target = (
        f"dish {dish_id}"
        if dish_id
        else (title or (f"task {task_gid}" if task_gid else "this task"))
    )
    phase = _clean(data.get("phase"))
    required = data.get("required_action")
    required_arguments = (
        required.get("arguments")
        if isinstance(required, Mapping) and isinstance(required.get("arguments"), Mapping)
        else {}
    )
    required_kind = _clean(required_arguments.get("kind"))
    if phase == "await_verification" or {"approve", "reject"} & command_set or required_kind == "verification":
        return f'Tell an agent: "Resume Verification for {target}."'
    return f'Tell an agent: "Resume Dish work for {target}."'


def _post_recovery_view(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    post = data.get("post_recovery")
    return post if isinstance(post, Mapping) else None


def _success_lines(command: str, data: Mapping[str, Any]) -> list[str]:
    effect = _clean(data.get("effect") or data.get("message"))
    if effect:
        lines = [effect]
    else:
        summaries = {
            "kill": (
                "The requested Dish-run replacement completed.",
            ),
            "authorize-governed-change": (
                "Authorization recorded. The task itself was not changed.",
                "Have the agent retry the same exact candidate.",
            ),
            "review-approve": (
                "The exact linked change bundle was approved and Dish attempted its separate mechanical application.",
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
                "The recovery step completed against the live task.",
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
    result: Mapping[str, Any], *, profile: str, verbose: bool = False, interactive: bool = False
) -> str:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    lines: list[str] = [f"Environment: {str(profile or 'prod').upper()}"]
    suppress_generic_actions = False
    suppress_generic_agent_actions = False

    title = _clean(data.get("task_title"))
    task_gid = _clean(result.get("task_gid") or data.get("task_gid"))
    operation_id = _clean(result.get("submission_id") or data.get("operation_id"))
    if title:
        lines.append(f"Dish: {title}")
    elif task_gid:
        lines.append(f"Dish: {task_gid}")
    if operation_id:
        lines.append(f"Operation: {operation_id}")
    lines.append("")

    ok = bool(result.get("ok"))
    command = _clean(result.get("command")) or "command"
    if command in {"review-queue", "review-inspect"} and ok:
        suppress_generic_actions = True
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
                summary = proposal.get("review_summary") if isinstance(proposal.get("review_summary"), Mapping) else {}
                issue = _clean(summary.get("issue") or proposal.get("proposal_reason"))
                if issue:
                    lines.append(f"   Issue: {issue}")
                blocker = _clean(summary.get("quantified_blocker"))
                if blocker:
                    lines.append(f"   Blocker: {blocker}")
                decision = _clean(summary.get("decision"))
                if decision and decision != issue:
                    lines.append(f"   Question: {decision}")
                hr_options = proposal.get("human_review_options") if isinstance(proposal.get("human_review_options"), list) else []
                for option in hr_options:
                    if not isinstance(option, Mapping):
                        continue
                    option_id = _clean(option.get("option_id")) or "?"
                    label_text = _clean(option.get("label")) or _clean(option.get("decision")) or "Option"
                    recommended = " (recommended)" if bool(option.get("recommended")) else ""
                    lines.append(f"   {option_id}. {label_text}{recommended}")
                if item_type == "human_review":
                    lines.append("   Other. Type a different instruction for the next agent.")
                next_step = _clean(summary.get("simplest_next_step"))
                if next_step and item_type not in {"semantic_proposal", "human_review"}:
                    lines.append(f"   Options: {next_step}")
                changes = proposal.get("changes") if isinstance(proposal.get("changes"), list) else []
                rendered_changes = 0
                for change in changes:
                    if not isinstance(change, Mapping) or "before" not in change or "after" not in change:
                        continue
                    field = _clean(change.get("field")) or "change"
                    lines.append(
                        f"   Change: {field}: {_compact_value(change.get('before'))} → "
                        f"{_compact_value(change.get('after'))}"
                    )
                    rendered_changes += 1
                    if rendered_changes == 2:
                        break
                if rendered_changes < len([change for change in changes if isinstance(change, Mapping)]):
                    lines.append(f"   Changes: {len(changes)} total; open the item for the complete bundle.")
                elif not rendered_changes:
                    fields = _clean(", ".join(str(item.get("field")) for item in changes if isinstance(item, Mapping)))
                    if fields:
                        lines.append(f"   Changes: {fields}")
                if not interactive:
                    lines.append(f"   Inspect: dish-admin review-inspect {proposal_id}")
                    if item_type == "semantic_proposal" and status == "PENDING":
                        # Approval binds the complete linked candidate bundle, which is shown only
                        # by review-inspect. Do not offer approval from this compact queue summary.
                        lines.append(f"   Reject template: dish-admin review-reject {proposal_id} --reason '<why>'")
                    elif item_type == "human_review":
                        lines.append(
                            f"   Choose: dish-admin review-approve {proposal_id} --choice A"
                        )
                        lines.append(
                            f"   Other: dish-admin review-approve {proposal_id} --choice other --reason '<instruction>'"
                        )
                    elif item_type == "verification_hold":
                        lines.append(f"   Release: dish-admin review-approve {proposal_id}")
            if items:
                lines.append("")
                lines.append(
                    "Select a number to inspect the exact decision or bundle."
                    if interactive
                    else "Queue numbers are accepted only for the current queue view; UUIDs are safer to copy or share."
                )
        else:
            proposal = data.get("review_item") if isinstance(data.get("review_item"), Mapping) else data.get("proposal") if isinstance(data.get("proposal"), Mapping) else {}
            proposal_id = _clean(proposal.get("review_id") or proposal.get("proposal_id")) or "unknown"
            item_type = _clean(proposal.get("item_type")) or "semantic_proposal"
            heading = "Human decision" if item_type == "human_review" else "Review"
            lines.append(f"{heading} {proposal_id}")
            lines.append(f"Status: {_clean(proposal.get('status')) or 'unknown'}")
            summary = proposal.get("review_summary") if isinstance(proposal.get("review_summary"), Mapping) else {}
            issue = _clean(summary.get("issue") or proposal.get("proposal_reason"))
            if issue:
                lines.append(f"Issue: {issue}")
            blocker = _clean(summary.get("quantified_blocker"))
            if blocker:
                lines.append(f"Blocker: {blocker}")
            decision = _clean(summary.get("decision"))
            if decision and decision != issue:
                lines.append(f"Question: {decision}")
            next_step = _clean(summary.get("simplest_next_step"))
            if next_step and item_type != "human_review":
                lines.append(f"Next: {next_step}")
            if item_type == "human_review":
                hr_options = proposal.get("human_review_options") if isinstance(proposal.get("human_review_options"), list) else []
                lines.append("")
                lines.append("Choices")
                if hr_options:
                    for option in hr_options:
                        if not isinstance(option, Mapping):
                            continue
                        option_id = _clean(option.get("option_id")) or "?"
                        label_text = _clean(option.get("label")) or "Option"
                        decision_text = _clean(option.get("decision"))
                        recommended = " — recommended" if bool(option.get("recommended")) else ""
                        lines.append(f"{option_id}. {label_text}{recommended}")
                        if decision_text and decision_text != label_text:
                            lines.append(f"   {decision_text}")
                        authorization = option.get("authorization")
                        if isinstance(authorization, Mapping):
                            lines.append(
                                f"   Authorizes {authorization.get('field')}: "
                                f"{_compact_value(authorization.get('before'))} → {_compact_value(authorization.get('after'))}"
                            )
                else:
                    lines.append("No agent-authored choices were stored for this older review.")
                lines.append("Other. Type a different instruction for the next agent.")

            changes = proposal.get("changes") if isinstance(proposal.get("changes"), list) else []
            if changes:
                lines.append("")
                lines.append("Governed changes")
                for item in changes:
                    if not isinstance(item, Mapping):
                        continue
                    lines.append(f"- {item.get('field')}: {json.dumps(item.get('before'), ensure_ascii=False)} -> {json.dumps(item.get('after'), ensure_ascii=False)}")

            linked = proposal.get("linked_changes") if isinstance(proposal.get("linked_changes"), list) else []
            if item_type == "semantic_proposal" and linked:
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

            if verbose:
                explanation = proposal.get("explanation") if isinstance(proposal.get("explanation"), Mapping) else {}
                detail_rows = []
                for label, key in (("Problem", "problem"), ("Cause", "cause"), ("Why this route", "why_not_ordinary_correction"), ("Recommended", "recommended_resolution"), ("Scope", "scope"), ("After approval", "after_success")):
                    value = _clean(explanation.get(key))
                    if value and value not in {issue, next_step}:
                        detail_rows.append((label, value))
                if detail_rows:
                    lines.append("")
                    lines.append("Detail")
                    lines.extend(f"{label}: {value}" for label, value in detail_rows)
            status = _clean(proposal.get("status"))
            if status == "pending" and item_type == "semantic_proposal" and not interactive:
                lines.append("")
                lines.append(f"Approve: dish-admin review-approve {proposal_id}")
                lines.append(f"Reject template: dish-admin review-reject {proposal_id} --reason '<why>'")
            elif status == "pending" and item_type == "human_review" and not interactive:
                lines.append("")
                options = proposal.get("human_review_options") if isinstance(proposal.get("human_review_options"), list) else []
                if options:
                    lines.append(f"Choose recommended A: dish-admin review-approve {proposal_id} --choice A")
                lines.append(
                    f"Other instruction: dish-admin review-approve {proposal_id} --choice other --reason '<instruction>'"
                )
            elif status == "pending" and item_type == "verification_hold" and not interactive:
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
    elif command == "audit" and ok:
        items = data.get("items") if isinstance(data.get("items"), list) else []
        counts = data.get("category_counts") if isinstance(data.get("category_counts"), Mapping) else {}
        lines.append("Cooking population audit")
        lines.append(
            f"Asana Cooking tasks: {int(data.get('asana_task_count') or 0)}; "
            f"Dish-known: {int(data.get('dish_known_count') or 0)}; "
            f"audited identities: {int(data.get('audited_task_count') or 0)}"
        )
        lines.append(
            "Healthy: {healthy}; Expected/manual: {expected}; Asana-only: {asana_only}; "
            "Dish-only/unavailable: {dish_only}; Inconsistent: {inconsistent}; Migration/repair: {migration}".format(
                healthy=int(counts.get("healthy_current") or 0),
                expected=int(counts.get("expected_external_lifecycle") or 0),
                asana_only=int(counts.get("asana_only") or 0),
                dish_only=int(counts.get("dish_known_asana_missing_or_unavailable") or 0),
                inconsistent=int(counts.get("real_inconsistency") or 0),
                migration=int(counts.get("needs_migration_repair") or 0),
            )
        )
        visible = items if verbose else [
            item for item in items
            if isinstance(item, Mapping)
            and item.get("category") not in {"healthy_current", "expected_external_lifecycle"}
        ]
        labels = {
            "healthy_current": "HEALTHY",
            "expected_external_lifecycle": "EXPECTED",
            "asana_only": "ASANA ONLY",
            "dish_known_asana_missing_or_unavailable": "DISH ONLY",
            "real_inconsistency": "INCONSISTENT",
            "needs_migration_repair": "MIGRATION/REPAIR",
        }
        if not visible:
            lines.append("")
            lines.append("No population difference or repair condition needs review.")
        for index, item in enumerate(visible, start=1):
            if not isinstance(item, Mapping):
                continue
            category = _clean(item.get("category")) or "unknown"
            title = _clean(item.get("task_title")) or _clean(item.get("task_gid")) or "Dish"
            lines.append("")
            lines.append(f"{index}. [{labels.get(category, category.upper())}] {title}")
            dish_id = _clean(item.get("dish_id"))
            if dish_id:
                lines.append(f"   Dish UUID: {dish_id}")
            section_name = _clean(item.get("section_name"))
            section_gid = _clean(item.get("section_gid"))
            if section_name or section_gid:
                lines.append(f"   Placement: {section_name or section_gid}")
            detail = _clean(item.get("detail"))
            if detail:
                lines.append(f"   {detail}")
            if verbose:
                reason = _clean(item.get("reason"))
                operation = _clean(item.get("operation_id"))
                if reason:
                    lines.append(f"   Rule: {reason}")
                if operation:
                    lines.append(f"   Operation: {operation}")
        if not verbose and (
            int(counts.get("healthy_current") or 0)
            or int(counts.get("expected_external_lifecycle") or 0)
        ):
            lines.append("")
            lines.append("Healthy/current rows are hidden; expected/manual rows are also hidden; use --verbose to list the complete population.")
    elif command in {"active", "active-leases"} and ok:
        leases = data.get("leases") if isinstance(data.get("leases"), list) else []
        counts = data.get("state_counts") if isinstance(data.get("state_counts"), Mapping) else {}
        lines.append(f"Unreleased actor leases: {len(leases)}")
        lines.append(
            "Active: {active}; Expired: {expired}; Revoked: {revoked}".format(
                active=int(counts.get("active") or 0),
                expired=int(counts.get("expired") or 0),
                revoked=int(counts.get("revoked") or 0),
            )
        )
        if not leases:
            lines.append("No Dish run currently holds an unreleased actor lease.")
        for index, lease in enumerate(leases, start=1):
            if not isinstance(lease, Mapping):
                continue
            title = _clean(lease.get("task_title")) or _clean(lease.get("dish_id")) or _clean(lease.get("task_gid")) or "Dish"
            state = (_clean(lease.get("authority_state")) or "unknown").upper()
            stage = _clean(lease.get("stage")) or "unknown stage"
            lines.append("")
            lines.append(f"{index}. [{state}] {title}")
            dish_id = _clean(lease.get("dish_id"))
            if dish_id:
                lines.append(f"   Dish UUID: {dish_id}")
            lease_began = _lease_began(lease.get("acquired_at"))
            if lease_began:
                lines.append(f"   Lease began: {lease_began}")
            if verbose:
                lines.append(f"   Stage: {stage}")
                lines.append(f"   Operation: {_clean(lease.get('operation_id')) or 'unknown'}")
                lines.append(f"   Owner: {_clean(lease.get('owner_id')) or 'unknown'}")
                lines.append(f"   Run: {_clean(lease.get('run_id')) or 'unknown'}")
                lines.append(f"   Lease: {_clean(lease.get('lease_id')) or 'unknown'}")
                lines.append(f"   Acquired: {_clean(lease.get('acquired_at')) or 'unknown'}")
                lines.append(f"   Renewed: {_clean(lease.get('renewed_at')) or 'unknown'}")
                lines.append(f"   Expires: {_clean(lease.get('expires_at')) or 'unknown'}")

    elif command in {"queue", "issues", "attention"} and ok:
        items = data.get("issue_items") if isinstance(data.get("issue_items"), list) else data.get("attention_items") if isinstance(data.get("attention_items"), list) else []
        needs_you = int(data.get("needs_you_count") or 0)
        system_count = int(data.get("system_count") or 0)
        noun = "dish" if needs_you == 1 else "dishes"
        lines.append("Marco queue")
        if needs_you:
            lines.append(f"{needs_you} {noun} below require you to resolve.")
        else:
            lines.append("Nothing currently requires you to resolve.")
        if system_count:
            lines.append(
                f"Use --verbose to list {system_count} auto-recoverable "
                f"dish{'es' if system_count != 1 else ''}."
            )
        visible_items = items if verbose else [
            item for item in items
            if isinstance(item, Mapping) and bool(item.get("needs_you"))
        ]
        group_labels = {
            "human_review": "Human review",
            "evidence": "Evidence needed",
            "change_review": "Change approval",
            "recovery": "Recovery / reconciliation",
            "system": "Auto-recoverable",
        }
        current_group = None
        for index, item in enumerate(visible_items, start=1):
            if not isinstance(item, Mapping):
                continue
            group = _clean(item.get("queue_group"))
            if group is None:
                kinds = {
                    _clean(signal.get("kind"))
                    for signal in (item.get("signals") if isinstance(item.get("signals"), list) else [])
                    if isinstance(signal, Mapping)
                }
                if {"human_decision", "human_hold"} & kinds:
                    group = "human_review"
                elif "evidence_hold" in kinds:
                    group = "evidence"
                elif "proposal_review" in kinds:
                    group = "change_review"
                elif _clean(item.get("category")) == "unsafe":
                    group = "recovery"
                elif bool(item.get("needs_you")):
                    group = "recovery"
                else:
                    group = "system"
            if group != current_group:
                lines.append("")
                lines.append(group_labels.get(group, "Other"))
                current_group = group
            title = _clean(item.get("task_title")) or _clean(item.get("dish_id")) or _clean(item.get("task_gid")) or "Dish"
            lines.append("")
            lines.append(f"{index}. {title}")
            dish_id = _clean(item.get("dish_id"))
            if dish_id:
                lines.append(f"   Dish UUID: {dish_id}")
            signals = item.get("signals") if isinstance(item.get("signals"), list) else []
            for signal in signals:
                if not isinstance(signal, Mapping):
                    continue
                signal_category = _clean(signal.get("category"))
                if not verbose and signal_category == "system":
                    continue
                summary = _clean(signal.get("summary"))
                detail = _clean(signal.get("detail"))
                command_text = _clean(signal.get("shell_command"))
                if summary:
                    lines.append(f"   {summary}")
                if detail and detail != summary:
                    lines.append(f"   {detail}")
                if command_text and not interactive:
                    lines.append(f"   Inspect: {command_text}")
        if visible_items and interactive:
            lines.append("")
            lines.append("Select a number to resolve or inspect it.")
        if verbose:
            lines.append("")
            lines.append(
                f"Durable workflow records: {int(data.get('checked_count') or 0)}; "
                f"live task inspections: {int(data.get('live_inspection_count') or 0)}"
            )
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
                    lines.append(f"   {_command_label(action)}: {shell}")
    elif command == "inspect" and ok:
        inspect_actions = _actions(data, (row for row in errors if isinstance(row, Mapping)))
        lines.append("Status")
        if _clean(data.get("status")) == "resting":
            if bool(data.get("ready_to_cook")):
                lines.append("Ready to cook.")
                lines.append("No workflow action or recovery is required.")
            else:
                lines.append("No workflow is currently running for this Dish.")
                lines.append("No recovery is required.")
        else:
            lines.append(_clean(data.get("problem")) or "No administrative blocker is recorded.")
            hold_question = _clean(data.get("hold_question"))
            if hold_question:
                lines.append(f"Question: {hold_question}")
            invocation = data.get("outstanding_invocation")
            replace_action = next(
                (
                    action
                    for action in inspect_actions
                    if _clean(action.get("command")) == "kill"
                ),
                None,
            )
            if isinstance(invocation, Mapping):
                run_id = _clean(invocation.get("run_id")) or "unknown"
                authority = (_clean(invocation.get("authority_state")) or "unknown").upper()
                stage = _clean(invocation.get("stage"))
                last_activity = _clean(invocation.get("last_activity_at"))
                lines.append(f"Outstanding run: {run_id} — {authority}")
                if stage:
                    lines.append(f"Stage: {stage}")
                if last_activity:
                    lines.append(f"Last Dish activity: {last_activity}")
            if replace_action is not None:
                lines.append("Choice: leave this run alone, or replace it.")
                shell = _command_from_action(replace_action)
                if shell:
                    lines.append(f"Replace template: {shell}")
                suppress_generic_actions = True
            else:
                abandonment_decision = _decision_first_abandonment_action(inspect_actions)
                recovery_action = _compact_recovery_action(inspect_actions)
                if abandonment_decision is not None:
                    action, question = abandonment_decision
                    lines.append(question)
                    shell = _command_from_action(action)
                    if shell:
                        lines.append(f"If yes, {_command_label(action)}: {shell}")
                    suppress_generic_actions = True
                elif recovery_action is not None:
                    shell = _command_from_action(recovery_action)
                    if shell:
                        lines.append(f"Run: {shell}")
                    suppress_generic_actions = True
                else:
                    waiting = _clean(data.get("waiting_for"))
                    if waiting:
                        lines.append(f"Waiting for: {waiting}")
                    operator_instruction = _clean(data.get("operator_instruction"))
                    if operator_instruction:
                        lines.append(f"Next: {operator_instruction}")
        diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), Mapping) else None
        if verbose and diagnostics is not None:
            lines.append("")
            lines.append("Technical diagnostics")
            content_head = diagnostics.get("content_head")
            if isinstance(content_head, Mapping):
                lines.append(
                    "Content head: schema={schema}; identity={identity}; version={version}; confirmed={confirmed}".format(
                        schema=_clean(content_head.get("schema_version")) or "unknown",
                        identity=_clean(content_head.get("last_confirmed_identity")) or "unknown",
                        version=_clean(content_head.get("last_confirmed_content_version_id")) or "unknown",
                        confirmed=_clean(content_head.get("confirmed_at")) or "unknown",
                    )
                )
            operation = diagnostics.get("operation")
            if isinstance(operation, Mapping):
                lines.append(
                    "Operation detail: kind={kind}; status={status}; phase={phase}; expected_section={section}; terminal={terminal}".format(
                        kind=_clean(operation.get("operation_kind")) or "unknown",
                        status=_clean(operation.get("status")) or "unknown",
                        phase=_clean(operation.get("phase")) or "unknown",
                        section=_clean(operation.get("expected_section_gid")) or "none",
                        terminal=_clean(operation.get("terminal_outcome")) or "none",
                    )
                )

            cycle_rows = diagnostics.get("verification_cycles") if isinstance(diagnostics.get("verification_cycles"), list) else []
            if cycle_rows:
                lines.append("Verification cycles")
                for row in cycle_rows:
                    if not isinstance(row, Mapping):
                        continue
                    lines.append(
                        "- #{number} {cycle}: run={run}; verifier={verifier}; outcome={outcome}; route={route}; completed={completed}".format(
                            number=row.get("cycle_number"),
                            cycle=_clean(row.get("cycle_id")) or "unknown",
                            run=_clean(row.get("run_id")) or "none",
                            verifier=_clean(row.get("verifier_agent")) or "none",
                            outcome=_clean(row.get("outcome")) or "open",
                            route=_clean(row.get("route")) or "none",
                            completed=_clean(row.get("completed_at")) or "no",
                        )
                    )

            request_rows = diagnostics.get("service_requests") if isinstance(diagnostics.get("service_requests"), list) else []
            execution_rows = diagnostics.get("operation_executions") if isinstance(diagnostics.get("operation_executions"), list) else []
            if request_rows or execution_rows:
                lines.append("Requests / executions")
                for row in request_rows:
                    if isinstance(row, Mapping):
                        lines.append(
                            f"- request {_clean(row.get('request_id')) or 'unknown'}: "
                            f"{_clean(row.get('command')) or 'unknown'} — {_clean(row.get('status')) or 'unknown'}"
                        )
                for row in execution_rows:
                    if isinstance(row, Mapping):
                        lines.append(
                            f"- execution {_clean(row.get('execution_id')) or 'unknown'}: "
                            f"{_clean(row.get('command')) or 'unknown'} — {_clean(row.get('status')) or 'unknown'}"
                        )

            write_rows = diagnostics.get("write_attempts") if isinstance(diagnostics.get("write_attempts"), list) else []
            move_rows = diagnostics.get("movement_attempts") if isinstance(diagnostics.get("movement_attempts"), list) else []
            if write_rows or move_rows:
                lines.append("External effects")
                for row in write_rows:
                    if isinstance(row, Mapping):
                        lines.append(
                            f"- write {_clean(row.get('attempt_id')) or 'unknown'}: "
                            f"{_clean(row.get('purpose')) or 'unknown'} — {_clean(row.get('outcome')) or 'unknown'}"
                        )
                for row in move_rows:
                    if isinstance(row, Mapping):
                        lines.append(
                            f"- move {_clean(row.get('attempt_id')) or 'unknown'}: "
                            f"{_clean(row.get('expected_section_gid')) or 'none'} → "
                            f"{_clean(row.get('intended_section_gid')) or 'unknown'} — "
                            f"{_clean(row.get('outcome')) or 'unknown'}"
                        )

            lease_rows = diagnostics.get("service_leases") if isinstance(diagnostics.get("service_leases"), list) else []
            revocation_rows = diagnostics.get("operation_run_revocations") if isinstance(diagnostics.get("operation_run_revocations"), list) else []
            if lease_rows or revocation_rows:
                lines.append("Authority")
                for row in lease_rows:
                    if isinstance(row, Mapping):
                        lines.append(
                            f"- lease {_clean(row.get('lease_id')) or 'unknown'}: "
                            f"owner={_clean(row.get('owner_id')) or 'unknown'}; "
                            f"run={_clean(row.get('run_id')) or 'unknown'}; "
                            f"released={_clean(row.get('released_at')) or 'no'}; "
                            f"expires={_clean(row.get('expires_at')) or 'unknown'}"
                        )
                for row in revocation_rows:
                    if isinstance(row, Mapping):
                        lines.append(
                            f"- revoked owner={_clean(row.get('owner_id')) or 'unknown'}; "
                            f"run={_clean(row.get('run_id')) or 'unknown'}; "
                            f"at={_clean(row.get('revoked_at')) or 'unknown'}; "
                            f"reason={_clean(row.get('reason')) or 'unknown'}"
                        )

            for label, key, id_key in (
                ("Semantic proposals", "semantic_proposals", "proposal_id"),
                ("Abandonments", "abandonment_attempts", "abandonment_id"),
                ("Safe reclaims", "safe_reclaims", "reclaim_id"),
                ("Successions", "operation_successions", "succession_id"),
                ("Inspect facts", "dish_inspect_facts", "fact_id"),
            ):
                rows = diagnostics.get(key) if isinstance(diagnostics.get(key), list) else []
                if not rows:
                    continue
                lines.append(label)
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    summary_bits = []
                    for field in ("status", "outcome", "transition_type", "stage", "run_id", "created_at"):
                        value = _clean(row.get(field))
                        if value:
                            summary_bits.append(f"{field}={value}")
                    lines.append(
                        f"- {_clean(row.get(id_key)) or 'unknown'}"
                        + (f": {'; '.join(summary_bits)}" if summary_bits else "")
                    )

            history_rows = diagnostics.get("operation_history") if isinstance(diagnostics.get("operation_history"), list) else []
            if history_rows:
                lines.append("Operation history")
                for row in history_rows:
                    if isinstance(row, Mapping):
                        lines.append(
                            f"- {_clean(row.get('operation_id')) or 'unknown'}: "
                            f"{_clean(row.get('operation_kind')) or 'unknown'} / "
                            f"{_clean(row.get('status')) or 'unknown'} / "
                            f"{_clean(row.get('phase')) or 'unknown'}"
                        )

            audit_rows = diagnostics.get("recent_audit_events") if isinstance(diagnostics.get("recent_audit_events"), list) else []
            if audit_rows:
                lines.append(f"Recent durable events (latest {int(diagnostics.get('recent_audit_event_limit') or 50)})")
                for row in audit_rows:
                    if isinstance(row, Mapping):
                        lines.append(
                            f"- {_clean(row.get('created_at')) or 'unknown'} "
                            f"{_clean(row.get('event_type')) or 'unknown'} "
                            f"[{_clean(row.get('result_code')) or 'n/a'}]"
                        )
    elif command in {"kill-all", "kill-all-expired"}:
        selected = int(data.get("selected_count") or 0)
        revoked = int(data.get("revoked_count") or data.get("killed_count") or 0)
        failed = int(data.get("failed_count") or 0)
        replacement_complete = int(data.get("replacement_complete_count") or 0)
        replacement_ready = int(data.get("replacement_ready_count") or 0)
        checkpoint_preserved = int(data.get("checkpoint_preserved_count") or 0)
        reconciliation_required = int(data.get("reconciliation_required_count") or 0)
        lines.append("Bulk run replacement")
        lines.append(f"Selected: {selected}; Revoked: {revoked}; Failed: {failed}")
        lines.append(
            f"Replacement complete: {replacement_complete}; "
            f"Reconciliation required: {reconciliation_required}"
        )
        if replacement_ready or checkpoint_preserved:
            lines.append(
                f"Replacement ready: {replacement_ready}; "
                f"Checkpoint preserved: {checkpoint_preserved}"
            )
        consequence = _clean(data.get("human_consequence"))
        if consequence:
            lines.append(consequence)
        rows = data.get("results") if isinstance(data.get("results"), list) else []
        outcome_labels = {
            "replacement_complete": "REPLACED",
            "replacement_ready": "READY",
            "checkpoint_preserved": "CHECKPOINT",
            "manual_reconciliation_required": "RECONCILIATION",
        }
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                continue
            title = _clean(row.get("task_title")) or _clean(row.get("task_gid")) or "Dish"
            outcome = _clean(row.get("outcome"))
            if bool(row.get("revoked")):
                status = outcome_labels.get(outcome or "", "REVOKED")
            else:
                status = _clean(row.get("code")) or "FAILED"
            lines.append(f"{index}. [{status}] {title}")
            if verbose:
                lines.append(f"   Operation: {_clean(row.get('operation_id')) or 'unknown'}")
                lines.append(f"   Run: {_clean(row.get('run_id')) or 'unknown'}")
                lines.append(f"   Lease: {_clean(row.get('lease_id')) or 'unknown'}")
        suppress_generic_actions = True
        suppress_generic_agent_actions = True
    elif command == "kill" and ok:
        consequence = _clean(data.get("human_consequence"))
        lines.append("Result")
        lines.append(consequence or "Dish completed the requested run replacement safely.")
        continuation = data.get("continuation")
        if isinstance(continuation, Mapping):
            waiting = _clean(continuation.get("waiting_for"))
            problem = _clean(continuation.get("problem"))
            if problem and problem != consequence:
                lines.append(f"Current Dish state: {problem}")
            if waiting:
                lines.append(f"Now waiting for: {waiting}")
        suppress_generic_actions = True
        suppress_generic_agent_actions = True
    elif ok:
        post_recovery = _post_recovery_view(data) if command in {"recover", "recover-lease"} else None
        if post_recovery is not None and bool(post_recovery.get("administrative_blocker")):
            lines.append("Recovery step completed")
            problem = _clean(post_recovery.get("problem"))
            if problem:
                lines.append(problem)
            post_actions = _actions(post_recovery)
            abandonment_decision = _decision_first_abandonment_action(post_actions)
            if abandonment_decision is not None:
                action, question = abandonment_decision
                lines.append(question)
                shell = _command_from_action(action)
                if shell:
                    lines.append(f"If yes, {_command_label(action)}: {shell}")
            suppress_generic_actions = True
            suppress_generic_agent_actions = True
        else:
            lines.append("Done")
            lines.extend(_success_lines(command, data))
    else:
        message = _clean(data.get("message")) or "The command could not be completed."
        consequence = next(
            (
                _clean(row.get("human_consequence"))
                for row in errors
                if isinstance(row, Mapping) and _clean(row.get("human_consequence"))
            ),
            None,
        )
        lines.append(f"Could not {command}")
        if consequence:
            lines.append(consequence)
        lines.append(message)

    actions = _actions(data, (row for row in errors if isinstance(row, Mapping)))
    if actions and not suppress_generic_actions:
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
                lines.append(f"   {_command_label(action)}: {shell}")

    handoff_data: Mapping[str, Any] = data
    post_recovery = _post_recovery_view(data)
    if command in {"recover", "recover-lease"}:
        if post_recovery is None:
            # A plain recovery result does not prove that every ownership-transfer
            # blocker is gone. Do not turn stale pre-recovery legal actions into a
            # confident agent handoff.
            suppress_generic_agent_actions = True
        else:
            handoff_data = post_recovery
            if bool(post_recovery.get("administrative_blocker")):
                suppress_generic_agent_actions = True

    agent_actions = handoff_data.get("agent_actions_now")
    if not isinstance(agent_actions, list):
        required = handoff_data.get("required_action")
        if isinstance(required, Mapping) and required.get("surface") == "connected-agent":
            agent_actions = [required]
        elif command not in {"recover", "recover-lease"}:
            agent_actions = result.get("allowed_actions")
    if isinstance(agent_actions, list) and agent_actions and not suppress_generic_agent_actions:
        handoff = _agent_handoff_line(
            result=result,
            data=handoff_data,
            commands=[_agent_command_name(item) for item in agent_actions],
        )
        if handoff:
            lines.append("")
            lines.append(handoff)

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
