"""Shared command transport types and exact task-membership helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
from typing import Any, Mapping, Protocol

from .constants import COOKING_PROJECT_GID
from .errors import DishRuleError


class CommandBackend(Protocol):
    def list_sections(self, project_gid: str) -> list[dict[str, Any]]: ...

    def read_task(self, task_gid: str) -> dict[str, Any]: ...

    def create_bare_task(
        self, *, title: str, project_gid: str, section_gid: str
    ) -> dict[str, Any]: ...

    def update_task_content(
        self, *, task_gid: str, title: str, notes: str
    ) -> None: ...

    def move_task_to_section(self, *, task_gid: str, section_gid: str) -> None: ...


@dataclass
class CommandTrace:
    task_gid: str | None = None
    submission_id: str | None = None
    state: str | None = None
    validation_scope: tuple[str, ...] = ()
    known_submission: bool = False
    audit_details: dict[str, Any] = field(default_factory=dict)
    actor_agent: str | None = None


def reject_undeclared_arguments(
    handler: Any, arguments: Mapping[str, Any]
) -> None:
    """Validate required and undeclared dispatcher arguments from the signature."""
    parameters = tuple(inspect.signature(handler).parameters.values())
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return
    declared_parameters = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
        and parameter.name not in {"self", "trace"}
    )
    declared = {
        parameter.name
        for parameter in declared_parameters
    }
    unexpected = sorted(set(arguments) - declared)
    if unexpected:
        field = unexpected[0]
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"{field} is not accepted by this command",
            rule="argument_unexpected",
            details={"field": field},
        )
    missing = next(
        (
            parameter.name
            for parameter in declared_parameters
            if parameter.default is inspect.Parameter.empty
            and parameter.name not in arguments
        ),
        None,
    )
    if missing is not None:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"{missing} is required",
            rule="argument_required",
            details={"field": missing},
        )


def _clean_required(value: Any, *, rule: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise DishRuleError("INVALID_ARGUMENT", f"{label} is required", rule=rule)
    return clean


def _gid(value: Any) -> str | None:
    if isinstance(value, Mapping):
        clean = str(value.get("gid") or "").strip()
    else:
        clean = str(value or "").strip()
    return clean or None


def _task_is_in_project(task: Mapping[str, Any], project_gid: str) -> bool:
    projects = task.get("projects") or []
    if isinstance(projects, list) and any(
        _gid(project) == project_gid for project in projects
    ):
        return True
    memberships = task.get("memberships") or []
    return isinstance(memberships, list) and any(
        isinstance(membership, Mapping)
        and _gid(membership.get("project")) == project_gid
        for membership in memberships
    )


def _task_section_gid(task: Mapping[str, Any], project_gid: str) -> str | None:
    memberships = task.get("memberships") or []
    if not isinstance(memberships, list):
        raise DishRuleError(
            "VALIDATION_FAILED",
            "task memberships are malformed",
            rule="task_membership_malformed",
        )
    matching = [
        membership
        for membership in memberships
        if isinstance(membership, Mapping)
        and _gid(membership.get("project")) == project_gid
    ]
    section_gids = {
        gid
        for membership in matching
        if (gid := _gid(membership.get("section"))) is not None
    }
    if len(section_gids) > 1:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "task has ambiguous Cooking section membership",
            rule="task_membership_ambiguous",
            details={"section_gids": sorted(section_gids)},
        )
    return next(iter(section_gids), None)


def _require_cooking_task(task: Mapping[str, Any], task_gid: str) -> None:
    if not _task_is_in_project(task, COOKING_PROJECT_GID):
        raise DishRuleError(
            "UNMANAGED_TASK",
            f"task {task_gid} is not in the Cooking project",
            rule="task_not_in_cooking",
        )
