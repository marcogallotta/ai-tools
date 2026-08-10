"""Dark-launch treatment derived from authoritative command metadata.

This module does not define workflow legality or a second command catalogue.
The PostgreSQL Stage A command contract owns retained/query command facts;
current Action/admin specs own the identities that are not yet represented by
that target contract. Dark launch adds only the exceptions that are genuinely
specific to shadow execution.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

Treatment = Literal["execute", "capture_only", "excluded"]


@dataclass(frozen=True)
class DarkLaunchTreatment:
    command_name: str
    treatment: Treatment
    reason: str

    @property
    def comparison_eligible(self) -> bool:
        """Whether an execute-mode envelope may produce parity evidence."""

        return self.treatment == "execute"

    @property
    def external_effects_allowed(self) -> Literal[False]:
        """Shadow treatment can never authorize an external effect."""

        return False


# These are the only dark-launch facts that cannot derive from the accepted
# Stage A command contract. The semantic-proposal/review entries remain here
# because that target contract still omits them; removing an exception requires
# first adding the authoritative target command fact.
_SHADOW_ONLY_OVERRIDES: dict[str, tuple[Treatment, str]] = {
    "create": ("capture_only", "pre-cutover create correlation is not qualified"),
    "recover": ("capture_only", "projection-attempt identity is target-specific"),
    "repair-destination": ("capture_only", "projection-attempt identity is target-specific"),
    "proposals": ("excluded", "read-only source semantic-proposal queue"),
    "apply-proposal": ("capture_only", "target semantic-proposal authority is not implemented"),
    "safe-reclaim": ("capture_only", "target safe-reclaim authority is not implemented"),
    "review-queue": ("excluded", "read-only source semantic-proposal queue"),
    "review-inspect": ("excluded", "read-only source semantic-proposal detail"),
    "review-approve": ("capture_only", "target semantic-proposal authority is not implemented"),
    "review-reject": ("capture_only", "target semantic-proposal authority is not implemented"),
    "kill": ("capture_only", "target high-level replacement orchestration is not implemented"),
}


def _derived_treatment(definition) -> tuple[Treatment, str]:
    if not definition.retained:
        return "excluded", "retired from PostgreSQL command authority"
    if definition.profile == "Q":
        reason = "read-only administrative query" if definition.principal == "admin" else "read-only query"
        return "excluded", reason
    return "execute", "retained target command is shadow-executable"


@lru_cache(maxsize=1)
def _build_treatments() -> dict[str, DarkLaunchTreatment]:
    # Keep the legacy service import boundary light. These metadata modules are
    # loaded only when dark launch actually needs the policy, after ordinary
    # service composition has completed.
    from dish_pg.command_contract import COMMAND_DEFINITIONS
    from dish_service.command_spec import ACTION_COMMAND_DEFINITIONS
    from dish_tool.admin_command_spec import ADMIN_COMMAND_SPECS

    current_surface_commands = set(ACTION_COMMAND_DEFINITIONS) | set(ADMIN_COMMAND_SPECS)
    stale_overrides = set(_SHADOW_ONLY_OVERRIDES) - current_surface_commands
    if stale_overrides:
        names = ", ".join(sorted(stale_overrides))
        raise ValueError(f"shadow-only treatment exceptions lack a current command identity: {names}")

    rows: dict[str, DarkLaunchTreatment] = {}
    for name, definition in COMMAND_DEFINITIONS.items():
        override = _SHADOW_ONLY_OVERRIDES.get(name)
        treatment, reason = override if override is not None else _derived_treatment(definition)
        rows[name] = DarkLaunchTreatment(name, treatment, reason)

    for name, (treatment, reason) in _SHADOW_ONLY_OVERRIDES.items():
        if name not in rows:
            rows[name] = DarkLaunchTreatment(name, treatment, reason)

    missing_treatment = current_surface_commands - set(rows)
    if missing_treatment:
        names = ", ".join(sorted(missing_treatment))
        raise ValueError(f"current commands lack target metadata or a shadow-only exception: {names}")
    return rows


class _TreatmentInventory(Mapping[str, DarkLaunchTreatment]):
    """Read-only compatibility view over the derived treatment inventory."""

    def __getitem__(self, command_name: str) -> DarkLaunchTreatment:
        return _build_treatments()[command_name]

    def __iter__(self) -> Iterator[str]:
        return iter(_build_treatments())

    def __len__(self) -> int:
        return len(_build_treatments())


DARK_LAUNCH_TREATMENTS: Mapping[str, DarkLaunchTreatment] = _TreatmentInventory()


def treatment_for(command_name: str) -> DarkLaunchTreatment:
    try:
        return _build_treatments()[command_name]
    except KeyError as exc:
        raise ValueError(f"dark-launch treatment is not registered: {command_name}") from exc
