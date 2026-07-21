"""Advisory-only managed-task checks for generic Asana title and note writes."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .constants import (
    AGENT_FAMILIES,
    COOKING_PROJECT_GID,
    DEFAULT_DB_PATH,
    EXCLUDED_SECTION_GIDS,
)
from .database import initialize_database, record_audit

_TASK_PATH = re.compile(r"^/tasks/([^/]+)$")
_TASKS_PATH = "/tasks"
_SUBTASKS_PATH = re.compile(r"^/tasks/([^/]+)/subtasks$")
_CONTENT_FIELDS = frozenset({"name", "notes", "html_notes"})


@dataclass(frozen=True)
class ManagementDecision:
    managed: bool
    resolution: str
    section_gid: str | None = None


def _gid(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("gid")
    clean = str(value or "").strip()
    return clean or None


def _unwrap_data(response: Any) -> Any:
    if isinstance(response, Mapping) and "data" in response:
        return response["data"]
    return response


def _known_actor() -> str | None:
    actor = str(os.environ.get("ASANA_AGENT") or "").strip().lower()
    return actor if actor in AGENT_FAMILIES else None


def _content_fields(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    return tuple(sorted(_CONTENT_FIELDS.intersection(payload)))


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


class AdvisoryGuard:
    """Consult managed-task status and log v1a bypasses without blocking writes."""

    def __init__(
        self,
        *,
        api_client: Any,
        db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    ) -> None:
        self.api_client = api_client
        self.db_path = Path(db_path).expanduser()
        self._conn = None

    def is_managed_task(self, task_gid: str) -> ManagementDecision:
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
            return ManagementDecision(True, "task_lookup_unresolved")

        if not isinstance(task, Mapping):
            return ManagementDecision(True, "task_lookup_unresolved")
        memberships = task.get("memberships")
        if not isinstance(memberships, Sequence) or isinstance(memberships, (str, bytes)):
            return ManagementDecision(True, "membership_unresolved")

        cooking_memberships = [
            membership
            for membership in memberships
            if isinstance(membership, Mapping)
            and _gid(membership.get("project")) == COOKING_PROJECT_GID
        ]
        if not cooking_memberships:
            return ManagementDecision(False, "outside_cooking")

        section_gids = {
            section_gid
            for membership in cooking_memberships
            if (section_gid := _gid(membership.get("section"))) is not None
        }
        if len(section_gids) != 1:
            return ManagementDecision(True, "section_unresolved")
        section_gid = next(iter(section_gids))
        managed = section_gid not in EXCLUDED_SECTION_GIDS
        return ManagementDecision(
            managed,
            "managed_section" if managed else "excluded_section",
            section_gid,
        )

    def is_managed_creation(
        self, *, project_gid: str, section_gid: str | None
    ) -> ManagementDecision:
        if str(project_gid) != COOKING_PROJECT_GID:
            return ManagementDecision(False, "outside_cooking")
        if not section_gid:
            return ManagementDecision(True, "section_unresolved")
        managed = str(section_gid) not in EXCLUDED_SECTION_GIDS
        return ManagementDecision(
            managed,
            "managed_section" if managed else "excluded_section",
            str(section_gid),
        )

    def _record(
        self,
        *,
        task_gid: str | None,
        command: str,
        decision: ManagementDecision,
        fields: Sequence[str] = ("notes",),
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if not decision.managed:
            return
        try:
            if self._conn is None:
                self._conn = initialize_database(self.db_path)
            details = {
                "command": command,
                "mode": "v1a_advisory",
                "resolution": decision.resolution,
                "section_gid": decision.section_gid,
                "fields": sorted(set(fields)),
            }
            details.update(dict(extra or {}))
            record_audit(
                self._conn,
                submission_id=None,
                task_gid=task_gid,
                event_type="generic_content_bypass",
                actor_agent=_known_actor(),
                details=details,
            )
        except Exception:
            # V1a is deliberately advisory: observability failure must not turn
            # the future v1b block on early by preventing the generic write.
            return

    def before_task_content(
        self,
        task_gid: str,
        *,
        command: str,
        fields: Sequence[str] = ("notes",),
        operation: str | None = None,
    ) -> None:
        decision = self.is_managed_task(task_gid)
        self._record(
            task_gid=task_gid,
            command=command,
            decision=decision,
            fields=fields,
            extra={"operation": operation} if operation else None,
        )

    def before_create_task(
        self,
        *,
        project_gid: str,
        section_gid: str | None,
        command: str,
        fields: Sequence[str] = ("notes",),
        operation: str | None = None,
    ) -> None:
        decision = self.is_managed_creation(
            project_gid=project_gid, section_gid=section_gid
        )
        self._record(
            task_gid=None,
            command=command,
            decision=decision,
            fields=fields,
            extra={
                "project_gid": project_gid,
                "intended_section_gid": section_gid,
                **({"operation": operation} if operation else {}),
            },
        )

    def before_create_subtask(
        self,
        *,
        parent_gid: str,
        command: str,
        fields: Sequence[str],
        operation: str | None = None,
    ) -> None:
        self.before_task_content(
            parent_gid,
            command=command,
            fields=fields,
            operation=operation or "create_subtask",
        )

    def before_raw(self, *, method: str, path: str, payload: Any) -> None:
        fields = _content_fields(payload)
        if not fields:
            return
        normalized_path = urlsplit(path).path.rstrip("/") or "/"
        task_match = _TASK_PATH.fullmatch(normalized_path)
        method = method.upper()
        if task_match and method not in {"GET", "DELETE"}:
            self.before_task_content(
                task_match.group(1), command="raw", fields=fields
            )
            return
        subtask_match = _SUBTASKS_PATH.fullmatch(normalized_path)
        if subtask_match and method == "POST":
            self.before_create_subtask(
                parent_gid=subtask_match.group(1),
                command="raw",
                fields=fields,
            )
            return
        if normalized_path == _TASKS_PATH and method == "POST" and isinstance(payload, Mapping):
            cooking, section_gid = _cooking_creation_target(payload)
            if cooking:
                self.before_create_task(
                    project_gid=COOKING_PROJECT_GID,
                    section_gid=section_gid,
                    command="raw",
                    fields=fields,
                )
