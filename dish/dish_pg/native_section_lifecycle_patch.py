"""Install the bounded native Section lifecycle extension on canonical PG authority."""
from __future__ import annotations

from typing import Any

from .native_section_lifecycle import (
    LIFECYCLE_COMMANDS,
    NativeSectionLifecycleError,
    apply_native_section_lifecycle,
)


def install_command_contract() -> None:
    """Register private admin lifecycle commands before the command port is imported."""

    from . import command_contract

    for name in ("create-section", "rename-section", "retire-section"):
        command_contract.COMMAND_DEFINITIONS.setdefault(
            name,
            command_contract.CommandDefinition(name, "L", "admin", True, False, False),
        )
    command_contract.ADMIN_COMMANDS = tuple(
        name
        for name, definition in command_contract.COMMAND_DEFINITIONS.items()
        if definition.principal in {"admin", "historical"} or definition.admin_exposed
    )
    command_contract.RETAINED_COMMANDS = tuple(
        name
        for name, definition in command_contract.COMMAND_DEFINITIONS.items()
        if definition.retained
    )


def install_port(port_cls: type) -> None:
    """Route lifecycle commands through the existing replay-bound command port."""

    original = port_cls._apply
    if getattr(original, "_native_section_lifecycle_patch", False):
        return

    from .command_port_common import CommandRuleError

    def patched(
        self,
        *,
        call,
        generation,
        binding,
        execution,
        task,
        operation,
    ) -> dict[str, Any]:
        if call.command_name not in LIFECYCLE_COMMANDS:
            return original(
                self,
                call=call,
                generation=generation,
                binding=binding,
                execution=execution,
                task=task,
                operation=operation,
            )
        try:
            return apply_native_section_lifecycle(
                self.session,
                command_name=call.command_name,
                arguments=call.arguments,
                generation=generation,
                execution=execution,
                now=call.now,
                uuid_factory=self.uuid_factory,
            )
        except NativeSectionLifecycleError as exc:
            raise CommandRuleError(
                exc.code,
                str(exc),
                http_status=exc.http_status,
                data=exc.data,
            ) from exc

    patched._native_section_lifecycle_patch = True
    port_cls._apply = patched


def install_release_head(release_module) -> None:
    """Advance the canonical migration-head identity for this implementation."""

    release_module.ALEMBIC_HEAD = "0053_native_section_lifecycle"
