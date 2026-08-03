"""Shared human/admin action specifications and shell rendering.

This module is the single source for agent-relayed ``dish-admin`` commands. It
keeps fixed workflow bindings separate from human-supplied text, renders shell
arguments safely, and preserves the legacy response keys while exposing one
structured ``human_action`` payload for newer callers.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class PromptField:
    name: str
    label: str
    placeholder: str


@dataclass(frozen=True)
class AdminActionSpec:
    kind: str
    command: str
    positional: tuple[str, ...] = ()
    options: tuple[tuple[str, str | None], ...] = ()
    summary: str = ""
    effect: str = ""
    prompt_fields: tuple[PromptField, ...] = ()
    after_success: Mapping[str, Any] | None = None
    details: tuple[str, ...] = ()
    context: Mapping[str, Any] | None = None
    recommended: bool = True

    @property
    def is_template(self) -> bool:
        return bool(self.prompt_fields)

    def shell_command(self) -> str:
        tokens: list[str] = ["dish-admin", self.command, *self.positional]
        for flag, value in self.options:
            tokens.append(flag)
            if value is not None:
                tokens.append(value)
        return shlex.join(tokens)

    def payload(self) -> dict[str, Any]:
        shell = self.shell_command()
        structured = {
            "kind": self.kind,
            "command": self.command,
            "arguments": {
                "positional": list(self.positional),
                "options": [
                    {"flag": flag, "value": value} for flag, value in self.options
                ],
            },
            "summary": self.summary,
            "effect": self.effect,
            "recommended": self.recommended,
            "requires_input": [
                {
                    "name": field.name,
                    "label": field.label,
                    "placeholder": field.placeholder,
                }
                for field in self.prompt_fields
            ],
            "after_success": dict(self.after_success or {}),
            "details": list(self.details),
            "context": dict(self.context or {}),
            "shell_command": shell,
        }
        payload: dict[str, Any] = {
            "human_action": structured,
            # Legacy callers historically read admin_command even when it contains
            # placeholders. Keep that field populated while the structured action
            # makes the template status explicit.
            "admin_command": shell,
            "admin_command_is_template": self.is_template,
            "admin_command_template": shell if self.is_template else None,
        }
        return payload


def exact_action(
    *,
    kind: str,
    command: str,
    positional: Iterable[object] = (),
    options: Sequence[tuple[str, object | None]] = (),
    summary: str,
    effect: str,
    after_success: Mapping[str, Any] | None = None,
    details: Sequence[str] = (),
    context: Mapping[str, Any] | None = None,
    recommended: bool = True,
) -> AdminActionSpec:
    return AdminActionSpec(
        kind=kind,
        command=command,
        positional=tuple(str(value) for value in positional),
        options=tuple(
            (flag, None if value is None else str(value)) for flag, value in options
        ),
        summary=summary,
        effect=effect,
        after_success=after_success,
        details=tuple(str(item) for item in details),
        context=context,
        recommended=recommended,
    )


def template_action(
    *,
    kind: str,
    command: str,
    positional: Iterable[object] = (),
    options: Sequence[tuple[str, object | None]] = (),
    prompt_fields: Sequence[PromptField],
    summary: str,
    effect: str,
    after_success: Mapping[str, Any] | None = None,
    details: Sequence[str] = (),
    context: Mapping[str, Any] | None = None,
    recommended: bool = True,
) -> AdminActionSpec:
    return AdminActionSpec(
        kind=kind,
        command=command,
        positional=tuple(str(value) for value in positional),
        options=tuple(
            (flag, None if value is None else str(value)) for flag, value in options
        ),
        summary=summary,
        effect=effect,
        prompt_fields=tuple(prompt_fields),
        after_success=after_success,
        details=tuple(str(item) for item in details),
        context=context,
        recommended=recommended,
    )


_EXEMPTION_LABELS = {
    "nutrition-kcal": "the normal 700–1,000 kcal main-meal range",
    "nutrition-protein": "the normal minimum 35 g protein target",
    "nutrition-fat": "the normal maximum 40 g fat limit",
}


def _bracket_tags(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(dict.fromkeys(re.findall(r"\[([a-z0-9-]+)\]", value)))


def governed_change_action(
    *,
    operation_id: str,
    field: str,
    before: object,
    after: object,
    reason_placeholder: str = "<why Marco approves this exact change>",
) -> AdminActionSpec:
    """Build the canonical human action for an exact governed-field approval."""

    before_json = json.dumps(before, sort_keys=True)
    after_json = json.dumps(after, sort_keys=True)
    before_tags = set(_bracket_tags(before))
    after_tags = set(_bracket_tags(after))
    added = sorted(after_tags - before_tags)
    removed = sorted(before_tags - after_tags)

    if field == "Exemptions" and (added or removed):
        parts: list[str] = []
        if added:
            parts.append("add " + ", ".join(f"[{tag}]" for tag in added))
        if removed:
            parts.append("remove " + ", ".join(f"[{tag}]" for tag in removed))
        change_text = f"Change this task's Exemptions: {'; '.join(parts)}."
        proposed_wording = f"Proposed Exemptions wording: {after}"
        consequences = [
            f"[{tag}] permits this exact candidate to depart from {_EXEMPTION_LABELS[tag]}."
            for tag in added
            if tag in _EXEMPTION_LABELS
        ]
    else:
        change_text = (
            f"Change the governed {field} field from {before_json} to {after_json}."
        )
        proposed_wording = None
        consequences = []

    details = (
        change_text,
        *(() if proposed_wording is None else (proposed_wording,)),
        *consequences,
        "Scope: this task, this operation, and these exact before/after values only.",
        "This command records authorization only; it does not edit the task or approve Verification.",
        "After success, the agent must retry the same unchanged candidate.",
    )
    return template_action(
        kind="authorize-governed-change",
        command="authorize-governed-change",
        positional=(operation_id,),
        options=(
            ("--field", field),
            ("--before", before_json),
            ("--after", after_json),
            ("--reason", reason_placeholder),
        ),
        prompt_fields=(
            PromptField(
                "reason",
                "Why Marco approves this exact change",
                reason_placeholder,
            ),
        ),
        summary=f"Authorize the exact change to {field}.",
        effect=(
            "Create one operation-bound authorization; it does not edit the task. "
            "The agent must retry the same candidate afterward."
        ),
        after_success={
            "agent_actions": ["retry the same candidate mutation"],
            "operation_id": operation_id,
        },
        details=details,
        context={
            "governed_change": {
                "field": field,
                "before": before,
                "after": after,
                "added_tokens": added,
                "removed_tokens": removed,
                "scope": "this task, operation, and exact proposed values",
                "modifies_task": False,
                "after_success": "retry the same unchanged candidate",
            }
        },
    )


def relay_text(spec: AdminActionSpec, *, instruction: str) -> str:
    if spec.is_template:
        intro = (
            "Tell Marco what decision or information is required, then give him "
            "this command after replacing the placeholder text:"
        )
    else:
        intro = "Tell Marco to run this exact command:"
    detail_text = ""
    if spec.details:
        detail_text = "\nBefore the command, explain plainly:\n" + "\n".join(
            f"- {detail}" for detail in spec.details
        )
    return f"{intro}{detail_text}\n{spec.shell_command()}\n{instruction}"
