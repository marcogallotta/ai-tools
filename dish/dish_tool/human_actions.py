"""Shared human/admin action specifications and shell rendering.

This module is the single source for agent-relayed ``dish-admin`` commands.  It
keeps fixed workflow bindings separate from human-supplied text, renders shell
arguments safely, and preserves the legacy response keys while exposing one
structured ``human_action`` payload for newer callers.
"""
from __future__ import annotations

from dataclasses import dataclass
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
        recommended=recommended,
    )


def relay_text(spec: AdminActionSpec, *, instruction: str) -> str:
    if spec.is_template:
        intro = "Tell Marco what decision or information is required, then give him this command after replacing the placeholder text:"
    else:
        intro = "Tell Marco to run this exact command:"
    return f"{intro}\n{spec.shell_command()}\n{instruction}"
