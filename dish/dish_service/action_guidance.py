"""Contextual operating guidance for the public GPT Action surface.

Workflow legality remains owned by ``allowed_actions`` and the authoritative
workflow view.  This module only renders caller guidance from the canonical
result that Dish has already produced, so state-specific operating instructions
travel with the response that makes them relevant.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _result_actions(result: Mapping[str, Any], data: Mapping[str, Any]) -> list[str]:
    """Read legal actions from one known result-envelope location."""

    for container, field in (
        (result, "allowed_actions"),
        (data, "allowed_actions"),
        (data, "legal_actions"),
    ):
        if field not in container:
            continue
        value = container[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return []
        return value
    return []


def _human_action_instruction(action: Mapping[str, Any]) -> str:
    shell = _text(action.get("shell_command"))
    suffix = (
        " Keep the exact shell command available, but do not print it unless Marco asks how to execute the action."
        if shell
        else " Do not synthesize an admin command."
    )
    kind = _text(action.get("kind")) or ""
    if kind in {"recover-expired-lease", "reconcile-uncertain-effect", "reconcile-before-ownership-transfer"}:
        return (
            "Keep the Marco-facing recovery handoff to one short blocker/status sentence and the "
            "fact that admin recovery is required. Do not explain leases, execution journals, ownership "
            "transfer, or recovery outcome mechanics unless Marco asks." + suffix
        )
    if kind == "supply-evidence":
        return (
            "Ask Marco the actual missing fact in plain English. Do not turn route/scope/date/reason "
            "fields, hold IDs, or evidence-recording mechanics into Marco's task." + suffix
        )
    return (
        "Keep the Marco-facing Human Review result compact: decision first, then any quantified blocker, "
        "then the simplest available options and what each does. Do not dump raw details, IDs, protocol "
        "mechanics, evidence notes, or resume state unless Marco asks for them." + suffix
    )


def action_agent_guidance(result: Mapping[str, Any]) -> dict[str, Any]:
    """Render immediate GPT guidance from one canonical Action result."""

    command = str(result.get("command") or "")
    code = str(result.get("code") or "")
    data_value = result.get("data")
    data = data_value if isinstance(data_value, Mapping) else {}
    actions = _result_actions(result, data)
    errors_value = result.get("errors")
    errors = errors_value if isinstance(errors_value, list) else []
    first_error = errors[0] if errors and isinstance(errors[0], Mapping) else {}
    error_rule = _text(first_error.get("rule"))

    instructions: list[str] = [
        "Treat this Dish result as workflow authority. Use only allowed_actions and exact "
        "continuation identifiers returned here; do not infer a transition or invent an "
        "operation, cycle, lease, hold, recovery target, or admin command."
    ]

    if code == "BACKEND_UNCERTAIN":
        instructions.append(
            "Stop. Preserve and report this result. Do not retry with a new request ID or "
            "attempt another mutation to bypass the uncertain state."
        )
    else:
        if error_rule == "governed_change_intent_confirmation_required":
            instructions.append(
                "No workflow or external effect was committed. Restore any incidental governed-text edit, or "
                "explicitly declare the intended governed field, then retry the corrected request with a fresh request ID."
            )
        if error_rule == "human_review_preflight_required":
            instructions.append(
                "Do the Human Review preflight yourself rather than dumping protocol questions on Marco. Explain the real issue "
                "in ordinary language. Supply one to six concrete plausible choices ordered best-first: choice A is always your "
                "recommended route, and the later admin UI also gives Marco a free-text Other choice. When a choice is an exact "
                "Exemptions authorization, send the complete before/after field values and use the canonical nutrition tag "
                "([nutrition-kcal], [nutrition-protein], or [nutrition-fat]) rather than prose such as 'protein exemption'. "
                "Use a reasonable defensible estimate, with assumptions stated, where exact values are unknowable; uncertainty alone is not a blocker. If you can "
                "already construct the exact governed fix, use a Large correction so Dish queues that exact proposal for review. "
                "Use Human Review only when a material Marco-only choice remains before an exact candidate can exist."
            )
        if error_rule in {
            "planning_handoff_requires_initial",
            "planning_handoff_requires_fresh_research_run",
        }:
            instructions.append(
                "Planning is complete. Continue Research with start(kind=initial) on this same task under a fresh "
                "client.run_id and fresh client.request_id. Omit prepared_operation_id: the completed Planning "
                "submission/operation is not a prepared successor, and the Planning run ID must not be reused."
            )
        legal_next = _text(data.get("legal_next_step"))
        if legal_next:
            instructions.append(legal_next)

        source_instruction = _text(data.get("instruction"))
        if source_instruction:
            instructions.append(source_instruction)
        directive = _text(data.get("directive"))
        if directive:
            instructions.append(directive)

        human_action = data.get("human_action")
        if isinstance(human_action, Mapping):
            instructions.append(_human_action_instruction(human_action))
        elif _text(data.get("required_admin_action")):
            instructions.append(
                "An admin continuation is required. Relay the exact resolver/instruction Dish "
                "returned and wait for Marco to confirm success before continuing."
            )

        agent_action = data.get("agent_action")
        if isinstance(agent_action, Mapping):
            action_command = _text(agent_action.get("command"))
            if action_command and action_command in actions:
                instructions.append(
                    f"Call {action_command} with the target arguments in "
                    "data.agent_action.arguments exactly as returned; add only caller/request "
                    "fields required by the current Action schema, and do not reconstruct target "
                    "identifiers."
                )

        required_start_kind = _text(data.get("required_start_kind"))
        if "start" in actions and required_start_kind:
            instructions.append(
                f"For the returned start continuation, use arguments.kind={required_start_kind} exactly."
            )

        if (
            command == "prepare"
            and data.get("handoff") == "planning-to-research"
            and "start" in actions
            and required_start_kind == "initial"
        ):
            instructions.append(
                "This normal Planning→Research handoff is non-prepared. Use the returned start target under a fresh "
                "client.run_id and fresh client.request_id, and omit prepared_operation_id."
            )

        retry = data.get("retry")
        if isinstance(retry, Mapping):
            retry_instruction = _text(retry.get("instruction"))
            if retry_instruction:
                instructions.append(retry_instruction)

        if command == "read":
            binding = data.get("identity_binding")
            dish_id = _text(binding.get("dish_id")) if isinstance(binding, Mapping) else None
            task_gid = _text(binding.get("task_gid")) if isinstance(binding, Mapping) else None
            if dish_id:
                continuation_identity = (
                    "Use the returned task_gid for task-scoped continuation;"
                    if task_gid
                    else "No task_gid is bound; use the returned dish_id for task-scoped continuation;"
                )
                instructions.append(
                    "This data.identity_binding is Dish's exact canonical Dish-to-task binding. "
                    f"{continuation_identity} do not rediscover "
                    "the Dish through sections or title matching, and never use dish_id as submission_id."
                )

        if command == "inspect" and error_rule == "operation_not_found":
            instructions.append(
                "submission_id is an operation/submission UUID, not a Dish UUID. If Marco supplied "
                "`dish <uuid>`, resolve that identity with read(dish_id=<uuid>) rather than browsing "
                "sections or guessing an operation."
            )

        if command == "section-tasks":
            instructions.append(
                "Section placement is discovery only, not workflow eligibility. Never use section/task "
                "browsing to resolve a canonical Dish UUID supplied by Marco; use read(dish_id=...) for "
                "that exact identity. If data.next_cursor is non-null, use that exact cursor for the "
                "next page; never invent or cross-reuse a cursor."
            )

        if command == "start" and "inspect" in actions:
            instructions.append(
                "Inspect this Verification candidate before making any semantic approval or rejection decision. "
                "Use a reasonable defensible estimate with stated assumptions for unknowable yield/portion values; do not "
                "invent false precision when no single estimate is defensible. Uncertainty is blocking only when it could "
                "materially change a safety, nutrition, settled-intent, or executability conclusion. A structured threshold "
                "blocker must give one defensible estimate versus the limit and the material excess/shortfall. For multi-task review, report one short block per task: "
                "outcome, quantified material issue if any, simplest fix, and only the Marco decision actually needed."
            )

        if "approve" in actions:
            instructions.append(
                "For Verification approval, correction is closed: use correction=none for a clean signoff; "
                "use correction=small only when supplying the complete Small corrected candidate as file_text. "
                "Do not send clean, minor, large, or any other correction value."
            )
        if "reject" in actions:
            instructions.append(
                "For Verification rejection, route is closed to large, evidence, or human-review. "
                "A Small correction is not a rejection route; apply it through approve with correction=small."
            )
        if command == "approve" and "submit" in actions:
            instructions.append("Call submit in this same run.")

        if command == "proposals" and "apply-proposal" in actions:
            instructions.append(
                "Apply only an approved proposal exactly as stored. Do not edit the parked candidate "
                "or reuse the proposer run ID."
            )

        if command == "apply-proposal" and result.get("ok"):
            instructions.append(
                "Proposal application installs the stored bundle and opens fresh Verification; it does "
                "not itself sign or submit the candidate. Follow the returned allowed_actions."
            )

        if error_rule == "semantic_proposal_queued":
            instructions.append(
                "Report only that this task needs Marco review, the material issue, and the exact governed field(s) proposed. "
                "Do not dump the candidate, linked evidence, IDs, or admin command unless Marco asks for detail."
            )

        if data.get("batch_may_continue") is True:
            instructions.append(
                "This item is safely parked. In an explicitly requested batch, continue unrelated "
                "eligible tasks rather than treating this item as a batch stop."
            )

    # Stable de-duplication keeps repeated source-specific guidance concise.
    unique = list(dict.fromkeys(instructions))
    return {
        "source": "dish",
        "state_specific": True,
        "instructions": unique,
    }


def attach_action_agent_guidance(result: dict[str, Any]) -> dict[str, Any]:
    """Attach contextual guidance to a canonical result returned via GPT Action."""

    data = result.setdefault("data", {})
    if not isinstance(data, dict):
        # Canonical Dish results always use an object here; fail closed rather than
        # replacing malformed application output silently.
        raise ValueError("Action result data must be an object")
    if (
        result.get("ok")
        and result.get("command") == "prepare"
        and data.get("handoff") == "planning-to-research"
        and data.get("required_start_kind") == "initial"
        and "start" in _result_actions(result, data)
    ):
        task_gid = _text(result.get("task_gid"))
        if task_gid is not None:
            data["agent_action"] = {
                "command": "start",
                "arguments": {"task_gid": task_gid, "kind": "initial"},
            }
            data["continuation_requirements"] = {
                "fresh_client_run_id": True,
                "fresh_client_request_id": True,
                "omit_arguments": ["prepared_operation_id"],
            }
    data["agent_guidance"] = action_agent_guidance(result)
    return result
