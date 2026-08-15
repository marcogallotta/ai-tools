"""Explicit owning-task identity for PR lifecycle authority."""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from pr_lifecycle_support import TASK_GID_RE

_OWNING_TASK_MARKER_RE = re.compile(
    r"<!--\s*dish-owning-task:v1\s+task=(?P<gid>\d{16})\s*-->",
    re.IGNORECASE,
)
_OWNING_TASK_LINE_RE = re.compile(
    r"(?im)^\s*(?:owning\s+task|asana(?:\s+task(?:\s+gid)?)?)\s*:\s*(?P<value>[^\n]+)$"
)
_TASK_ASANA_LINE_RE = re.compile(
    r"(?im)^\s*task\s*:\s*asana\b\s*:?\s*(?P<value>[^\n]+)$"
)


class TaskReferences(list[str]):
    """All referenced task IDs plus separately resolved owner authority."""

    def __init__(
        self,
        values: Iterable[str],
        *,
        owning_task_id: str | None = None,
        owning_task_error: str | None = None,
    ) -> None:
        super().__init__(values)
        self.owning_task_id = owning_task_id
        self.owning_task_error = owning_task_error


def owning_task_identity_from_pr(pr: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Resolve one explicit owner; arbitrary related task references never qualify."""
    body = str(pr.get("body") or "")
    declared: list[str] = [match.group("gid") for match in _OWNING_TASK_MARKER_RE.finditer(body)]
    for pattern in (_OWNING_TASK_LINE_RE, _TASK_ASANA_LINE_RE):
        for match in pattern.finditer(body):
            gids = sorted(set(TASK_GID_RE.findall(match.group("value"))))
            if len(gids) != 1:
                if gids:
                    return None, f"owning-task declaration is ambiguous: {gids!r}"
                continue
            declared.append(gids[0])
    unique = sorted(set(declared))
    if len(unique) == 1:
        return unique[0], None
    if len(unique) > 1:
        return None, f"multiple conflicting explicit owning-task declarations: {unique!r}"
    return None, "explicit owning Asana task is missing"


def task_ids_from_pr(pr: Mapping[str, Any]) -> list[str]:
    text = "\n".join([str(pr.get("body") or ""), str(pr.get("title") or "")])
    owner, error = owning_task_identity_from_pr(pr)
    return TaskReferences(
        sorted(set(TASK_GID_RE.findall(text))),
        owning_task_id=owner,
        owning_task_error=error,
    )


def owning_task_identity_from_references(task_ids: list[str]) -> tuple[str | None, str | None]:
    return (
        getattr(task_ids, "owning_task_id", None),
        getattr(task_ids, "owning_task_error", "explicit owning Asana task is missing"),
    )
