"""Read the single owner's saved planner without calendar mutation authority."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import PrivateAttr

DATABASE_ENV = "DISH_MCP_PLANNER_DATABASE_URL"
SEMANTICS = (
    "Latest saved editable workspace, not an approval or unsaved browser changes. "
    "The outer revision is the database revision; state.revision is document metadata. "
    "batches.cookDate describes cooking; portions.date describes serving. "
    "Tracks 0 and 1 are movable columns, not fixed lunch/dinner. "
    "Undated portions are in Holding and do not cover dated meals. "
    "travel.away is the separate away board; travelDates marks absence at home. "
    "Read extras and preparations too; preparation tasks are not additional mains. "
    "Read-only inspection does not authorize moving or changing anything."
)


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message, "retryable": retryable}


class PlannerReadTool(Tool):
    _dsn: str = PrivateAttr()

    def __init__(self, dsn: str) -> None:
        super().__init__(
            name="dish_planner_read",
            description=(
                "Read Marco's current saved planner from its live database, including home/away "
                "boards, Holding, bakes and preparations. Use this for the current schedule "
                "instead of roadmap files or exports. Takes no arguments and cannot edit anything. "
                + SEMANTICS
            ),
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        )
        self._dsn = dsn

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        if arguments:
            result = _failure("INVALID_ARGUMENTS", "This read-only tool takes no arguments.")
        else:
            result = await self._read()
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(result))], structured_content=result,
        )

    async def _read(self) -> dict[str, Any]:
        if not self._dsn:
            return _failure("PLANNER_NOT_CONFIGURED", "The saved planner connection is not configured.")
        try:
            async with (
                await psycopg.AsyncConnection.connect(
                    self._dsn,
                    connect_timeout=3,
                    options="-c default_transaction_read_only=on -c statement_timeout=3000",
                ) as connection,
                connection.cursor() as cursor,
            ):
                await cursor.execute(
                    "SELECT revision, document, updated_at FROM cook_planner.plans WHERE id = %s",
                    ("main",),
                )
                row = await cursor.fetchone()
        except (psycopg.Error, ValueError):
            # Connection strings and server diagnostics can contain credentials. Never return them.
            return _failure(
                "PLANNER_UNAVAILABLE", "The saved planner could not be read. No fallback was used.",
                retryable=True,
            )
        if row is None:
            return _failure("PLANNER_NOT_INITIALIZED", "No saved planner exists yet.")
        revision, state, updated_at = row
        if (
            type(revision) is not int or revision < 1
            or not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("batches"), list)
            or not isinstance(state.get("portions"), list)
            or not isinstance(updated_at, datetime)
        ):
            return _failure(
                "PLANNER_UNSUPPORTED_STATE",
                "The saved planner has an unsupported shape or version. No fallback was used.",
            )
        return {
            "ok": True, "code": "OK", "retryable": False,
            "source": "live saved cook planner", "revision": revision,
            "updated_at": updated_at.isoformat(),
            "read_at": datetime.now(timezone.utc).isoformat(),
            "semantics": SEMANTICS, "state": state,
        }


def build_planner_tool() -> PlannerReadTool:
    return PlannerReadTool(os.environ.get(DATABASE_ENV, "").strip())
