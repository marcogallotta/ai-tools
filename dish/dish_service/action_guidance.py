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
    actions = [str(item) for item in (result.get("allowed_actions") or [])]
    data_value = result.get("data")
    data = data_value if isinstance(data_value, Mapping) else {}
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
                "recommended route, and the later admin UI also gives Marco a free-text Other choice. Use a reasonable defensible "
                "estimate, with assumptions stated, where exact values are unknowable; uncertainty alone is not a blocker. If you can "
                "already construct the exact governed fix, use a Large correction so Dish queues that exact proposal for review. "
                "Use Human Review only when a material Marco-only choice remains before an exact candidate can exist."
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

        retry = data.get("retry")
        if isinstance(retry, Mapping):
            retry_instruction = _text(retry.get("instruction"))
            if retry_instruction:
                instructions.append(retry_instruction)

        if command == "section-tasks":
            instructions.append(
                "Section placement is discovery only, not workflow eligibility. Confirm the task "
                "through Dish read/start. If data.next_cursor is non-null, use that exact cursor "
                "for the next page; never invent or cross-reuse a cursor."
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
    data["agent_guidance"] = action_agent_guidance(result)
    return result
