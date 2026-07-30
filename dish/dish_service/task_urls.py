"""Strict parsing for the narrow Asana task URLs accepted by Dish admin tools."""

from __future__ import annotations

from urllib.parse import urlsplit

from dish_tool.errors import DishRuleError

from .identifiers import require_asana_gid


def _invalid(message: str) -> DishRuleError:
    return DishRuleError(
        "INVALID_ARGUMENT",
        message,
        rule="asana_task_url_invalid",
        retryable=False,
        details={"field": "target"},
    )


def task_gid_from_url(value: str) -> str:
    """Return the task GID from one of the two admin-supported Asana URL forms."""

    if not isinstance(value, str) or not value:
        raise _invalid("target must be an Asana task URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise _invalid("target is not a valid Asana task URL") from exc

    if parsed.scheme.lower() != "https":
        raise _invalid("Asana task URL must use HTTPS")
    if parsed.hostname is None or parsed.hostname.lower() != "app.asana.com":
        raise _invalid("Asana task URL must use app.asana.com")
    if parsed.username is not None or parsed.password is not None:
        raise _invalid("Asana task URL must not contain user information")
    if port not in {None, 443}:
        raise _invalid("Asana task URL must not use a non-default port")
    if parsed.query or parsed.fragment:
        raise _invalid("Asana task URL must not contain a query or fragment")
    if "%" in parsed.path:
        raise _invalid("Asana task URL must not contain encoded path components")

    segments = parsed.path.split("/")
    if segments and segments[0] == "":
        segments = segments[1:]
    if not segments or any(segment == "" for segment in segments):
        raise _invalid("Asana task URL path is not a supported task-link form")

    if len(segments) == 3 and segments[0] == "0":
        project_gid = require_asana_gid(segments[1], field="project_gid")
        task_gid = require_asana_gid(segments[2], field="task_gid")
        del project_gid
        return task_gid

    if (
        len(segments) == 6
        and segments[0] == "1"
        and segments[2] == "project"
        and segments[4] == "task"
    ):
        workspace_gid = require_asana_gid(segments[1], field="workspace_gid")
        project_gid = require_asana_gid(segments[3], field="project_gid")
        task_gid = require_asana_gid(segments[5], field="task_gid")
        del workspace_gid, project_gid
        return task_gid

    raise _invalid("Asana task URL path is not a supported task-link form")
