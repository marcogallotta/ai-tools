"""Install the bounded native Section lifecycle extension on canonical PG authority."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from . import stage3_models as wf
from .native_section_lifecycle import (
    LIFECYCLE_COMMANDS,
    NativeSectionLifecycleError,
    apply_native_section_lifecycle,
)
from .native_section_lifecycle_models import SectionCatalogRebindReceipt


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


def _rebind_open_operations(self, *, call, generation, execution, result: dict[str, Any]) -> dict[str, Any]:
    predecessor_catalog_version_id = uuid.UUID(str(call.arguments["expected_catalog_version_id"]))
    catalog_version_id = uuid.UUID(str(result["catalog_version_id"]))
    statement = (
        select(wf.WorkflowOperation)
        .where(
            wf.WorkflowOperation.generation_id == generation.generation_id,
            wf.WorkflowOperation.lifecycle == "open",
        )
        .order_by(wf.WorkflowOperation.task_id)
    )
    if self.session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update().execution_options(populate_existing=True)
    operations = tuple(self.session.scalars(statement))
    for operation in operations:
        if operation.catalog_version_id != predecessor_catalog_version_id:
            raise NativeSectionLifecycleError(
                "STALE_OPERATION_CATALOG_BINDING",
                "open workflow operation catalog binding moved during Section lifecycle mutation",
                data={"operation_id": str(operation.operation_id)},
            )
        receipt = self.session.get(
            SectionCatalogRebindReceipt,
            (generation.generation_id, operation.task_id, catalog_version_id),
        )
        if receipt is None:
            raise NativeSectionLifecycleError(
                "CATALOG_REBIND_RECEIPT_MISSING",
                "open workflow operation has no matching Dish catalog-rebind receipt",
                data={"operation_id": str(operation.operation_id)},
            )
        operation.catalog_version_id = catalog_version_id
    if operations:
        self.session.add(
            wf.GovernedAuditEvent(
                audit_event_id=self.uuid_factory(),
                generation_id=generation.generation_id,
                request_id=call.request_id,
                command_execution_id=execution.execution_id,
                task_id=None,
                operation_id=None,
                event_type="section_catalog_operation_rebinds",
                actor=f"{call.owner_id}:{call.run_id}",
                payload={
                    "predecessor_catalog_version_id": str(predecessor_catalog_version_id),
                    "catalog_version_id": str(catalog_version_id),
                    "operation_ids": [str(operation.operation_id) for operation in operations],
                },
                occurred_at=call.now,
            )
        )
    self.session.flush()
    stale_open_count = self.session.scalar(
        select(func.count())
        .select_from(wf.WorkflowOperation)
        .where(
            wf.WorkflowOperation.generation_id == generation.generation_id,
            wf.WorkflowOperation.lifecycle == "open",
            wf.WorkflowOperation.catalog_version_id != catalog_version_id,
        )
    )
    if stale_open_count:
        raise NativeSectionLifecycleError(
            "AUTHORITATIVE_READBACK_FAILED",
            "open workflow operations did not rebind to the successor catalog",
        )
    return {**result, "rebound_open_operation_count": len(operations)}


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
            result = apply_native_section_lifecycle(
                self.session,
                command_name=call.command_name,
                arguments=call.arguments,
                generation=generation,
                execution=execution,
                now=call.now,
                uuid_factory=self.uuid_factory,
            )
            return _rebind_open_operations(
                self,
                call=call,
                generation=generation,
                execution=execution,
                result=result,
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
