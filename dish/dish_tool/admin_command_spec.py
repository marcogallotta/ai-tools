"""Typed registry for every private Dish administration command."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet


class AdminTargetKind(str, Enum):
    NONE = "none"
    OPERATION_OR_TASK = "operation-or-task"
    TASK = "task"
    REVIEW = "review"
    ABANDONMENT = "abandonment"
    LEASE_OR_TASK = "lease-or-task"
    BACKUP = "backup"


class CliArgumentShape(str, Enum):
    NONE = "none"
    POSITIONAL = "positional"
    OPTIONS_ONLY = "options-only"


class AdminTransport(str, Enum):
    CLI = "cli"
    PRIVATE_HTTP = "private-http"


@dataclass(frozen=True)
class AdminCommandSpec:
    name: str
    target_kind: AdminTargetKind
    identifier_field: str | None
    cli_argument_shape: CliArgumentShape
    backend_required: bool
    operation_lease_required: bool
    lease_free: bool
    supported_transports: FrozenSet[AdminTransport]
    resolve_operation_target: bool = False
    operation_scoped: bool = False
    run_id_field: bool = False

    def __post_init__(self) -> None:
        if self.operation_lease_required and self.lease_free:
            raise ValueError(f"{self.name}: lease-required command cannot be lease-free")
        if self.identifier_field is None and self.cli_argument_shape is CliArgumentShape.POSITIONAL:
            raise ValueError(f"{self.name}: positional command requires an identifier field")


_BOTH = frozenset({AdminTransport.CLI, AdminTransport.PRIVATE_HTTP})


def _spec(
    name: str,
    *,
    target: AdminTargetKind = AdminTargetKind.NONE,
    identifier: str | None = None,
    shape: CliArgumentShape = CliArgumentShape.NONE,
    backend: bool = False,
    lease: bool = False,
    lease_free: bool = False,
    resolve_operation: bool = False,
    operation_scoped: bool = False,
    run_id: bool = False,
) -> AdminCommandSpec:
    return AdminCommandSpec(
        name=name,
        target_kind=target,
        identifier_field=identifier,
        cli_argument_shape=shape,
        backend_required=backend,
        operation_lease_required=lease,
        lease_free=lease_free,
        supported_transports=_BOTH,
        resolve_operation_target=resolve_operation,
        operation_scoped=operation_scoped,
        run_id_field=run_id,
    )


ADMIN_COMMAND_SPECS = {
    spec.name: spec
    for spec in (
        _spec("attention", backend=True, lease_free=True),
        _spec("review-queue", lease_free=True),
        _spec("review-inspect", target=AdminTargetKind.REVIEW, identifier="proposal_id", shape=CliArgumentShape.POSITIONAL, lease_free=True),
        _spec("review-approve", target=AdminTargetKind.REVIEW, identifier="proposal_id", shape=CliArgumentShape.POSITIONAL, backend=True, operation_scoped=True),
        _spec("review-reject", target=AdminTargetKind.REVIEW, identifier="proposal_id", shape=CliArgumentShape.POSITIONAL, backend=True, operation_scoped=True),
        _spec("inspect", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease_free=True, resolve_operation=True, operation_scoped=True),
        _spec("holds", backend=True, lease_free=True),
        _spec("recover", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease=True, resolve_operation=True, operation_scoped=True),
        _spec("repair-destination", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease=True, resolve_operation=True, operation_scoped=True, run_id=True),
        _spec("discard", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease=True, resolve_operation=True, operation_scoped=True),
        _spec("abandon-operation", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease_free=True, resolve_operation=True, operation_scoped=True),
        _spec("reconcile-abandonment", target=AdminTargetKind.ABANDONMENT, identifier="abandonment_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease_free=True, operation_scoped=True),
        _spec("migrate", target=AdminTargetKind.TASK, identifier="task_gid", shape=CliArgumentShape.POSITIONAL, backend=True),
        _spec("reopen-planning", target=AdminTargetKind.TASK, identifier="task_gid", shape=CliArgumentShape.POSITIONAL, backend=True),
        _spec("reopen", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease=True, resolve_operation=True, operation_scoped=True),
        _spec("supply-evidence", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease=True, resolve_operation=True, operation_scoped=True),
        _spec("record-human-decision", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease=True, resolve_operation=True, operation_scoped=True),
        _spec("resolved", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease=True, resolve_operation=True, operation_scoped=True),
        _spec("authorize-governed-change", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, lease_free=True, resolve_operation=True, operation_scoped=True, run_id=True),
        _spec("recover-lease", target=AdminTargetKind.OPERATION_OR_TASK, identifier="submission_id", shape=CliArgumentShape.POSITIONAL, backend=True, lease=True, resolve_operation=True),
        _spec("expire-lease", target=AdminTargetKind.LEASE_OR_TASK, identifier="target", shape=CliArgumentShape.POSITIONAL, lease_free=True),
        _spec("backup-create", shape=CliArgumentShape.OPTIONS_ONLY, lease_free=True),
        _spec("backup-restore", target=AdminTargetKind.BACKUP, identifier="backup_id", shape=CliArgumentShape.POSITIONAL, lease_free=True),
    )
}

ADMIN_COMMANDS = frozenset(ADMIN_COMMAND_SPECS)
CLI_OPERATION_IDENTIFIER_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.identifier_field == "submission_id"
)
RESOLVED_OPERATION_TARGET_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.resolve_operation_target
)
OPERATION_SCOPED_ADMIN_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.operation_scoped
)
LEASE_FREE_ADMIN_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.lease_free
)
OPERATION_LEASE_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.operation_lease_required
)
RUN_ID_ADMIN_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.run_id_field
)
