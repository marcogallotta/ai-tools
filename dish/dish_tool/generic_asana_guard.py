"""Fail-closed guard for generic Asana mutations involving governed Cooking tasks.

The generic ``tools/asana`` CLI remains useful for reads and for writes outside the
governed Cooking workflow.  It must never become an alternate mutation path for a
task whose authority belongs to Dish.  This module performs read-only Asana lookups
and blocks before the generic tool sends a write.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .constants import COOKING_PROJECT_GID, EXCLUDED_SECTION_GIDS

_TASK_PATH = re.compile(r"^/tasks/([^/]+)$")
_SUBTASKS_PATH = re.compile(r"^/tasks/([^/]+)/subtasks$")
_TASK_PROJECT_PATH = re.compile(r"^/tasks/([^/]+)/(?:addProject|removeProject)$")
_SECTION_ADD_TASK_PATH = re.compile(r"^/sections/([^/]+)/addTask$")
_TASKS_PATH = "/tasks"


@dataclass(frozen=True)
class GuardDecision:
    blocked: bool
    resolution: str
    section_gid: str | None = None


class CookingMutationBlocked(RuntimeError):
    """Raised before a generic Asana mutation crosses the Dish authority boundary."""

    def __init__(self, *, command: str, resolution: str, task_gid: str | None = None,
                 section_gid: str | None = None) -> None:
        self.command = command
        self.resolution = resolution
        self.task_gid = task_gid
        self.section_gid = section_gid
        target = task_gid or section_gid or "Cooking"
        super().__init__(
            f"generic Asana mutation blocked for governed Cooking target {target} "
            f"({resolution}); use dish/dish-admin"
        )


def _gid(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("gid")
    clean = str(value or "").strip()
    return clean or None


def _unwrap_data(response: Any) -> Any:
    if isinstance(response, Mapping) and "data" in response:
        return response["data"]
    return response


def _cooking_creation_target(payload: Mapping[str, Any]) -> tuple[bool, str | None]:
    section_gids: set[str] = set()
    cooking = False

    memberships = payload.get("memberships")
    if isinstance(memberships, Sequence) and not isinstance(memberships, (str, bytes)):
        for membership in memberships:
            if not isinstance(membership, Mapping):
                continue
            if _gid(membership.get("project")) != COOKING_PROJECT_GID:
                continue
            cooking = True
            if section_gid := _gid(membership.get("section")):
                section_gids.add(section_gid)

    projects = payload.get("projects")
    if isinstance(projects, Sequence) and not isinstance(projects, (str, bytes)):
        cooking = cooking or any(_gid(project) == COOKING_PROJECT_GID for project in projects)
    elif _gid(projects) == COOKING_PROJECT_GID:
        cooking = True

    if _gid(payload.get("project")) == COOKING_PROJECT_GID:
        cooking = True
    if cooking and (section_gid := _gid(payload.get("section"))):
        section_gids.add(section_gid)

    return cooking, next(iter(section_gids)) if len(section_gids) == 1 else None


class CookingMutationGuard:
    """Block generic writes when the target is, or may be, Dish-governed.

    Lookup failures fail closed for task- and section-targeted writes.  The guard is
    intentionally read-only and never opens the Dish database; protocol authority and
    audit evidence remain exclusively inside ``dish-service``.
    """

    def __init__(self, *, api_client: Any) -> None:
        self.api_client = api_client

    def classify_task(self, task_gid: str) -> GuardDecision:
        try:
            import asana

            response = asana.TasksApi(self.api_client).get_task(
                task_gid,
                {
                    "opt_fields": (
                        "memberships.project.gid,memberships.section.gid,"
                        "memberships.section.name"
                    )
                },
            )
            task = _unwrap_data(response)
        except Exception:
            return GuardDecision(True, "task_lookup_unresolved")

        if not isinstance(task, Mapping):
            return GuardDecision(True, "task_lookup_unresolved")
        memberships = task.get("memberships")
        if not isinstance(memberships, Sequence) or isinstance(memberships, (str, bytes)):
            return GuardDecision(True, "membership_unresolved")

        cooking_memberships = [
            membership
            for membership in memberships
            if isinstance(membership, Mapping)
            and _gid(membership.get("project")) == COOKING_PROJECT_GID
        ]
        if not cooking_memberships:
            return GuardDecision(False, "outside_cooking")

        section_gids = {
            section_gid
            for membership in cooking_memberships
            if (section_gid := _gid(membership.get("section"))) is not None
        }
        if not section_gids:
            return GuardDecision(True, "cooking_section_unresolved")
        managed = sorted(section_gids - set(EXCLUDED_SECTION_GIDS))
        if managed:
            return GuardDecision(True, "managed_section", managed[0])
        return GuardDecision(False, "excluded_section", sorted(section_gids)[0])

    def classify_section(self, section_gid: str) -> GuardDecision:
        section_gid = str(section_gid).strip()
        if section_gid in EXCLUDED_SECTION_GIDS:
            return GuardDecision(False, "excluded_section", section_gid)
        try:
            import asana

            response = asana.SectionsApi(self.api_client).get_section(
                section_gid, {"opt_fields": "project.gid"}
            )
            section = _unwrap_data(response)
        except Exception:
            return GuardDecision(True, "section_lookup_unresolved", section_gid)
        if not isinstance(section, Mapping):
            return GuardDecision(True, "section_lookup_unresolved", section_gid)
        project_gid = _gid(section.get("project"))
        if project_gid is None:
            return GuardDecision(True, "section_project_unresolved", section_gid)
        if project_gid != COOKING_PROJECT_GID:
            return GuardDecision(False, "outside_cooking", section_gid)
        return GuardDecision(True, "managed_section", section_gid)

    @staticmethod
    def _raise_if_blocked(
        decision: GuardDecision,
        *,
        command: str,
        task_gid: str | None = None,
        section_gid: str | None = None,
    ) -> None:
        if decision.blocked:
            raise CookingMutationBlocked(
                command=command,
                resolution=decision.resolution,
                task_gid=task_gid,
                section_gid=section_gid or decision.section_gid,
            )

    def before_task_mutation(
        self,
        task_gid: str,
        *,
        command: str,
        fields: Sequence[str] = (),
        operation: str | None = None,
    ) -> None:
        del fields, operation
        self._raise_if_blocked(
            self.classify_task(task_gid), command=command, task_gid=str(task_gid)
        )

    def before_move(self, *, task_gid: str, section_gid: str, command: str) -> None:
        self._raise_if_blocked(
            self.classify_task(task_gid), command=command, task_gid=str(task_gid)
        )
        self._raise_if_blocked(
            self.classify_section(section_gid),
            command=command,
            task_gid=str(task_gid),
            section_gid=str(section_gid),
        )

    def before_create_task(
        self,
        *,
        project_gid: str,
        section_gid: str | None,
        command: str,
        fields: Sequence[str] = (),
        operation: str | None = None,
    ) -> None:
        del fields, operation
        if str(project_gid) != COOKING_PROJECT_GID:
            return
        if section_gid and str(section_gid) in EXCLUDED_SECTION_GIDS:
            return
        resolution = "managed_section" if section_gid else "cooking_section_unresolved"
        raise CookingMutationBlocked(
            command=command,
            resolution=resolution,
            section_gid=None if section_gid is None else str(section_gid),
        )

    def before_create_subtask(
        self,
        *,
        parent_gid: str,
        command: str,
        fields: Sequence[str] = (),
        operation: str | None = None,
    ) -> None:
        self.before_task_mutation(
            parent_gid,
            command=command,
            fields=fields,
            operation=operation or "create_subtask",
        )

    def before_raw(self, *, method: str, path: str, payload: Any) -> None:
        method = method.upper()
        if method == "GET":
            return
        normalized = urlsplit(path).path.rstrip("/") or "/"

        task_match = _TASK_PATH.fullmatch(normalized)
        if task_match:
            self.before_task_mutation(task_match.group(1), command="raw")
            return

        subtask_match = _SUBTASKS_PATH.fullmatch(normalized)
        if subtask_match and method == "POST":
            self.before_create_subtask(
                parent_gid=subtask_match.group(1), command="raw"
            )
            return

        section_match = _SECTION_ADD_TASK_PATH.fullmatch(normalized)
        if section_match and method == "POST" and isinstance(payload, Mapping):
            task_gid = _gid(payload.get("task"))
            if task_gid is None:
                raise CookingMutationBlocked(
                    command="raw", resolution="movement_task_unresolved",
                    section_gid=section_match.group(1),
                )
            self.before_move(
                task_gid=task_gid,
                section_gid=section_match.group(1),
                command="raw",
            )
            return

        project_match = _TASK_PROJECT_PATH.fullmatch(normalized)
        if project_match and method == "POST":
            task_gid = project_match.group(1)
            if isinstance(payload, Mapping):
                project_gid = _gid(payload.get("project"))
                section_gid = _gid(payload.get("section"))
                if project_gid == COOKING_PROJECT_GID:
                    self.before_create_task(
                        project_gid=project_gid,
                        section_gid=section_gid,
                        command="raw",
                    )
            self.before_task_mutation(task_gid, command="raw")
            return

        if normalized == _TASKS_PATH and method == "POST" and isinstance(payload, Mapping):
            cooking, section_gid = _cooking_creation_target(payload)
            if cooking:
                self.before_create_task(
                    project_gid=COOKING_PROJECT_GID,
                    section_gid=section_gid,
                    command="raw",
                )
