"""PostgreSQL Human Review admission for Verification rejects.

Formal Human Review parks a task until Marco records a decision, so Dish admits it
only for the closed set of choices that genuinely require Marco's recorded
authority. Everything else is a conversational question: the agent asks Marco in
chat and continues from his answer without a Dish hold.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .command_port_common import CommandRuleError

HUMAN_REVIEW_CATEGORIES: dict[str, str] = {
    "nutrition_exemption": "a nutrition exemption",
    "reverse_authenticated_decision": (
        "reversing an authenticated Decision, Lock, or Exemption"
    ),
    "governed_authorization": (
        "a change Dish returns as needing Marco's authorization (a Planning field "
        "outside Marco's kickoff Decision)"
    ),
}

HUMAN_REVIEW_SCOPE = (
    "Human Review is only for "
    + "; ".join(HUMAN_REVIEW_CATEGORIES.values())
    + ". Ask Marco this in chat and continue from his answer."
)

_BLOCKER_FIELDS = (
    "blocker_metric",
    "blocker_actual",
    "blocker_limit",
    "blocker_delta",
    "blocker_unit",
    "blocker_basis",
)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _preflight_error(message: str, **data: Any) -> CommandRuleError:
    return CommandRuleError(
        "HUMAN_REVIEW_PREFLIGHT_REQUIRED",
        message,
        http_status=400,
        data={
            **data,
            "instruction": (
                "No hold was opened. Supply the decision-ready Human Review packet and retry "
                "with a fresh request ID: human_review_basis is the one question Marco is "
                "deciding; human_review_options lists one to six concrete choices ordered "
                "best-first, with choice A your recommendation. If the question is not "
                "one of the Human Review categories, do not retry: " + HUMAN_REVIEW_SCOPE
            ),
            "retry": {
                "fresh_request_id": True,
                "human_review_category": "<one of "
                + ", ".join(HUMAN_REVIEW_CATEGORIES)
                + ">",
                "human_review_basis": "<one concise question Marco is actually deciding>",
                "human_review_options": [
                    {
                        "label": "<recommended route>",
                        "decision": "<exact decision to record if Marco chooses A>",
                    },
                    {
                        "label": "<another plausible route>",
                        "decision": "<exact decision to record if Marco chooses B>",
                    },
                ],
            },
        },
    )


def _validated_options(raw_options: object) -> list[dict[str, Any]]:
    if not isinstance(raw_options, list) or not 1 <= len(raw_options) <= 6:
        raise _preflight_error(
            "Human Review requires between one and six concrete choices"
        )
    options: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_options):
        if not isinstance(raw, Mapping):
            raise _preflight_error(
                "Human Review choices must be objects", option_index=index
            )
        if raw.get("authorization") is not None:
            raise CommandRuleError(
                "HUMAN_REVIEW_OPTION_AUTHORIZATION_UNSUPPORTED",
                "Human Review choices cannot carry governed-field authorizations",
                http_status=400,
                data={
                    "option_index": index,
                    "instruction": (
                        "No hold was opened. When you can construct the exact governed "
                        "change, reject with route=large and the complete corrected "
                        "file_text: Dish queues that exact change as a semantic proposal for "
                        "Marco to approve. Otherwise describe each choice in its decision "
                        "text and retry without authorization, using a fresh request ID."
                    ),
                },
            )
        label = _text(raw.get("label"))
        decision = _text(raw.get("decision"))
        if not label or not decision:
            raise _preflight_error(
                "Each Human Review choice needs a label and a concrete decision",
                option_index=index,
            )
        options.append(
            {
                "option_id": chr(ord("A") + index),
                "label": label,
                "decision": decision,
                "recommended": index == 0,
            }
        )
    return options


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(str(value).strip())
        except (TypeError, ValueError):
            value = math.nan
    number = float(value)
    if not math.isfinite(number):
        raise CommandRuleError(
            "QUANTIFIED_BLOCKER_INVALID",
            "blocker_actual, blocker_limit, and blocker_delta must be numeric",
            http_status=400,
            data={"field": field},
        )
    return number


def validated_quantified_blocker(
    arguments: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return one complete numeric threshold blocker, or None when none was given."""

    present = {
        name
        for name in _BLOCKER_FIELDS
        if arguments.get(name) is not None and str(arguments.get(name)).strip() != ""
    }
    if not present:
        return None
    missing = sorted(set(_BLOCKER_FIELDS) - present)
    if missing:
        raise CommandRuleError(
            "QUANTIFIED_BLOCKER_INCOMPLETE",
            "quantified blocker fields must be supplied together",
            http_status=400,
            data={"missing": missing},
        )
    actual = _number(arguments["blocker_actual"], "blocker_actual")
    limit = _number(arguments["blocker_limit"], "blocker_limit")
    delta = _number(arguments["blocker_delta"], "blocker_delta")
    expected_delta = actual - limit
    if abs(expected_delta - delta) > 1e-9:
        raise CommandRuleError(
            "QUANTIFIED_BLOCKER_DELTA_MISMATCH",
            "blocker_delta must equal blocker_actual minus blocker_limit",
            http_status=400,
            data={"expected_delta": expected_delta, "actual_delta": delta},
        )
    return {
        "metric": str(arguments["blocker_metric"]).strip(),
        "actual": actual,
        "limit": limit,
        "delta": delta,
        "unit": str(arguments["blocker_unit"]).strip(),
        "basis": str(arguments["blocker_basis"]).strip(),
    }


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


def _question_text(*, reason: str, packet: Mapping[str, Any]) -> str:
    lines = [
        reason,
        "",
        f"Category: {HUMAN_REVIEW_CATEGORIES[packet['category']]}",
        f"Decision needed: {packet['basis']}",
    ]
    blocker = packet.get("quantified_blocker")
    if blocker:
        unit = f" {blocker['unit']}" if blocker["unit"] else ""
        sign = "+" if blocker["delta"] >= 0 else ""
        lines.append(
            f"Blocker: {blocker['metric']} {_format_number(blocker['actual'])}{unit} vs "
            f"limit {_format_number(blocker['limit'])}{unit} "
            f"({sign}{_format_number(blocker['delta'])}{unit}; basis: {blocker['basis']})"
        )
    lines.append("Options:")
    for option in packet["options"]:
        marker = " (recommended)" if option["recommended"] else ""
        lines.append(
            f"{option['option_id']}{marker}. {option['label']} — {option['decision']}"
        )
    if packet.get("repairs_considered"):
        lines.append(f"Repairs considered: {packet['repairs_considered']}")
    return "\n".join(lines)


def human_review_admission(
    arguments: Mapping[str, Any], *, reason: str
) -> dict[str, Any]:
    """Validate a Verification Human Review reject before any hold is opened.

    Returns the normalized decision packet plus the requirement question text that
    Marco sees in the admin review.
    """

    category = _text(arguments.get("human_review_category"))
    if category not in HUMAN_REVIEW_CATEGORIES:
        raise CommandRuleError(
            "HUMAN_REVIEW_NOT_ALLOWED",
            HUMAN_REVIEW_SCOPE,
            http_status=400,
            data={
                "allowed_categories": list(HUMAN_REVIEW_CATEGORIES),
                "actual_category": category or None,
                "instruction": (
                    "No hold was opened. "
                    + HUMAN_REVIEW_SCOPE
                    + " Retry route=human-review only if the question is one of those "
                    "categories, naming it in human_review_category with a fresh request ID."
                ),
            },
        )
    resume_status = arguments.get("resume_status")
    if resume_status not in {None, "", "pending-verification"}:
        raise CommandRuleError(
            "INVALID_RESUME_STATUS",
            "a Verification Human Review always resumes to pending-verification",
            http_status=400,
            data={"actual": resume_status, "allowed": ["pending-verification"]},
        )
    basis = _text(arguments.get("human_review_basis"))
    if not basis:
        raise _preflight_error(
            "Human Review requires human_review_basis: the one question Marco is deciding"
        )
    options = _validated_options(arguments.get("human_review_options"))
    quantified_blocker = validated_quantified_blocker(arguments)
    if category == "nutrition_exemption" and quantified_blocker is None:
        raise CommandRuleError(
            "QUANTIFIED_BLOCKER_REQUIRED",
            "a nutrition-exemption Human Review needs its quantified blocker",
            http_status=400,
            data={
                "required": list(_BLOCKER_FIELDS),
                "instruction": (
                    "No hold was opened. State one defensible estimate (blocker_actual), the "
                    "limit (blocker_limit), blocker_delta = actual - limit, the metric, unit, "
                    "and the calculation basis, then retry with a fresh request ID."
                ),
            },
        )
    packet: dict[str, Any] = {
        "category": category,
        "basis": basis,
        "options": options,
        "quantified_blocker": quantified_blocker,
        "repairs_considered": _text(arguments.get("repairs_considered")) or None,
    }
    packet["question"] = _question_text(reason=reason, packet=packet)
    return packet
