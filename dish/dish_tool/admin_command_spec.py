"""Typed registry for shared private Dish administration command facts."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdminCommandSpec:
    name: str
    lease_free: bool = False
    resolve_operation_target: bool = False
    operation_scoped: bool = False
    run_id_field: bool = False


def _spec(
    name: str,
    *,
    lease_free: bool = False,
    resolve_operation: bool = False,
    operation_scoped: bool = False,
    run_id: bool = False,
) -> AdminCommandSpec:
    return AdminCommandSpec(
        name=name,
        lease_free=lease_free,
        resolve_operation_target=resolve_operation,
        operation_scoped=operation_scoped,
        run_id_field=run_id,
    )


ADMIN_COMMAND_SPECS = {
    spec.name: spec
    for spec in (
        _spec("queue", lease_free=True),
        _spec("issues", lease_free=True),  # hidden compatibility alias
        _spec("attention", lease_free=True),  # hidden compatibility alias
        _spec("audit", lease_free=True),
        _spec("active", lease_free=True),
        _spec("active-leases", lease_free=True),  # hidden compatibility alias
        _spec("review-queue", lease_free=True),  # hidden compatibility/detail view
        _spec("review-inspect", lease_free=True),
        _spec("review-approve", operation_scoped=True),
        _spec("review-reject", operation_scoped=True),
        _spec("inspect", lease_free=True),
        _spec("kill", lease_free=True),
        _spec("kill-all", lease_free=True),
        _spec("kill-all-expired", lease_free=True),
        _spec("holds", lease_free=True),
        _spec("recover", resolve_operation=True, operation_scoped=True),
        _spec(
            "repair-destination",
            resolve_operation=True,
            operation_scoped=True,
            run_id=True,
        ),
        _spec("discard", resolve_operation=True, operation_scoped=True),
        _spec(
            "abandon-operation",
            lease_free=True,
            resolve_operation=True,
            operation_scoped=True,
        ),
        _spec(
            "reconcile-abandonment",
            lease_free=True,
            operation_scoped=True,
        ),
        _spec("migrate"),
        _spec("reopen-planning"),
        _spec("reopen", resolve_operation=True, operation_scoped=True),
        _spec("supply-evidence", resolve_operation=True, operation_scoped=True),
        _spec(
            "record-human-decision",
            resolve_operation=True,
            operation_scoped=True,
        ),
        _spec("resolved", resolve_operation=True, operation_scoped=True),
        _spec(
            "authorize-governed-change",
            lease_free=True,
            resolve_operation=True,
            operation_scoped=True,
            run_id=True,
        ),
        _spec("recover-lease", resolve_operation=True),
        _spec("expire-lease", lease_free=True),
        _spec("backup-create", lease_free=True),
        _spec("backup-restore", lease_free=True),
    )
}

ADMIN_COMMANDS = frozenset(ADMIN_COMMAND_SPECS)
RESOLVED_OPERATION_TARGET_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.resolve_operation_target
)
OPERATION_SCOPED_ADMIN_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.operation_scoped
)
LEASE_FREE_ADMIN_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.lease_free
)
RUN_ID_ADMIN_COMMANDS = frozenset(
    name for name, spec in ADMIN_COMMAND_SPECS.items() if spec.run_id_field
)
