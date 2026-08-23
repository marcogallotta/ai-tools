"""Read-only population-audit command support for ``dish-admin``."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol

from .errors import DishRuleError
from .results import result_envelope

if TYPE_CHECKING:
    from .admin import AdminTrace


class _AdminAuditContext(Protocol):
    backend: Any | None
    conn: sqlite3.Connection
    release_loader: Callable[[], Any] | None


_AUDIT_CATEGORY_ORDER = {
    "real_inconsistency": 0,
    "needs_migration_repair": 1,
    "dish_known_asana_missing_or_unavailable": 2,
    "asana_only": 3,
    "expected_external_lifecycle": 4,
    "healthy_current": 5,
}


def _audit_project_tasks(backend: Any, *, project_gid: str) -> list[dict[str, Any]]:
    """Return one stable de-duplicated project listing without per-task reads."""
    tasks: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page, next_cursor = backend.list_tasks_for_project(project_gid, cursor=cursor)
        for raw in page:
            if not isinstance(raw, Mapping):
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "Asana returned malformed project task data",
                    rule="backend_response_malformed",
                )
            task_gid = str(raw.get("gid") or "").strip()
            if not task_gid:
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "Asana returned a project task without a GID",
                    rule="backend_response_malformed",
                )
            tasks[task_gid] = dict(raw)
        if next_cursor is None:
            break
        if next_cursor in seen_cursors:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "Asana repeated a project-task pagination cursor",
                rule="backend_pagination_loop",
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return list(tasks.values())


def _audit_known_task_gids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """SELECT task_gid FROM task_content_state
           UNION
           SELECT task_gid FROM operations
           UNION
           SELECT task_gid FROM service_leases"""
    ).fetchall()
    return {
        str(row["task_gid"])
        for row in rows
        if str(row["task_gid"] or "").strip()
    }


def _audit_operation_rows(
    conn: sqlite3.Connection, *, task_gid: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM operations
             WHERE task_gid=?
             ORDER BY created_at DESC, operation_id DESC""",
        (task_gid,),
    ).fetchall()


def _audit_item(
    *,
    task_gid: str,
    title: str | None,
    category: str,
    reason: str,
    section_gid: str | None,
    section_name: str | None,
    operation: sqlite3.Row | None,
    dish_known: bool,
    asana_present: bool,
    detail: str,
) -> dict[str, Any]:
    from .identifiers import stable_dish_uuid_for_asana_identity

    try:
        dish_id = str(stable_dish_uuid_for_asana_identity("task", task_gid))
    except ValueError:
        dish_id = None
    return {
        "dish_id": dish_id,
        "task_gid": task_gid,
        "task_title": title,
        "category": category,
        "reason": reason,
        "detail": detail,
        "dish_known": dish_known,
        "asana_present": asana_present,
        "section_gid": section_gid,
        "section_name": section_name,
        "operation_id": None if operation is None else operation["operation_id"],
        "operation_status": None if operation is None else operation["status"],
        "operation_phase": None if operation is None else operation["phase"],
        "expected_section_gid": (
            None if operation is None else operation["expected_section_gid"]
        ),
    }


def _command_audit(
    self: _AdminAuditContext, *, trace: AdminTrace
) -> dict[str, Any]:
    """Audit Cooking discovery population against durable Dish-owned workflow integrity.

    Asana section, due date, and project membership are operator-managed during the
    pre-cutover period. They may be reported as context but never make a Dish
    inconsistent.
    """
    if self.backend is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "population audit requires backend access",
            rule="admin_audit_unavailable",
        )

    from .command_support import _task_is_in_project, _task_section_gid
    from .constants import COOKING_PROJECT_GID
    from .models import SectionRegistry

    sections = self.backend.list_sections(COOKING_PROJECT_GID)
    registry = SectionRegistry.from_sections(sections)
    section_names = {
        str(item.get("gid") or "").strip(): str(item.get("name") or "").strip()
        for item in sections
        if str(item.get("gid") or "").strip()
    }
    project_tasks = _audit_project_tasks(
        self.backend, project_gid=COOKING_PROJECT_GID
    )
    asana_by_gid = {
        str(item.get("gid")): item
        for item in project_tasks
        if str(item.get("gid") or "").strip()
    }
    known_task_gids = _audit_known_task_gids(self.conn)
    release = None if self.release_loader is None else self.release_loader()
    current_schema_version = (
        None
        if release is None
        else str(release.schema_version or "").strip() or None
    )

    items: list[dict[str, Any]] = []

    for task_gid in sorted(set(asana_by_gid) | known_task_gids):
        listed = asana_by_gid.get(task_gid)
        dish_known = task_gid in known_task_gids
        state = self.conn.execute(
            "SELECT * FROM task_content_state WHERE task_gid=?", (task_gid,)
        ).fetchone()
        operations = (
            _audit_operation_rows(self.conn, task_gid=task_gid) if dish_known else []
        )
        active_operations = [
            row for row in operations if row["status"] in {"open", "uncertain"}
        ]
        latest_operation = operations[0] if operations else None
        operation_for_detail = (
            active_operations[0]
            if len(active_operations) == 1
            else latest_operation
        )
        migration_required = bool(
            (
                state is not None
                and current_schema_version is not None
                and str(state["schema_version"]) != current_schema_version
            )
            or any(
                bool(row["migration_reconciliation_required"])
                for row in active_operations
            )
        )

        if listed is None:
            try:
                live_raw = self.backend.read_task(task_gid)
            except DishRuleError as exc:
                fallback_title = (
                    None
                    if state is None
                    else str(state["last_confirmed_title"] or "").strip() or None
                )
                items.append(
                    _audit_item(
                        task_gid=task_gid,
                        title=fallback_title,
                        category="dish_known_asana_missing_or_unavailable",
                        reason=(
                            "asana_task_missing"
                            if exc.code == "NOT_FOUND"
                            else "asana_task_unavailable"
                        ),
                        section_gid=None,
                        section_name=None,
                        operation=operation_for_detail,
                        dish_known=True,
                        asana_present=False,
                        detail=(
                            "Dish has durable records for this task, but Asana no longer returns the task."
                            if exc.code == "NOT_FOUND"
                            else "Dish has durable records for this task, but its Asana state "
                            f"could not be read ({exc.code}/{exc.rule})."
                        ),
                    )
                )
                continue

            title = str(live_raw.get("name") or "").strip() or (
                None
                if state is None
                else str(state["last_confirmed_title"] or "").strip() or None
            )
            in_project = _task_is_in_project(live_raw, COOKING_PROJECT_GID)
            section_gid = None
            if in_project:
                try:
                    section_gid = _task_section_gid(live_raw, COOKING_PROJECT_GID)
                except DishRuleError:
                    # Placement shape is observational in the pre-cutover audit.
                    section_gid = None
            category = (
                "needs_migration_repair" if migration_required else "healthy_current"
            )
            reason = (
                "durable_schema_or_migration_reconciliation"
                if migration_required
                else "operator_managed_asana_organization"
            )
            detail = (
                "Dish durable state is older than the current supported task schema or "
                "explicitly requires migration reconciliation."
                if migration_required
                else "Dish knows this task. Section, due date, and project membership are "
                "operator-managed and are not audit inconsistencies."
            )
            items.append(
                _audit_item(
                    task_gid=task_gid,
                    title=title,
                    category=category,
                    reason=reason,
                    section_gid=section_gid,
                    section_name=section_names.get(section_gid or ""),
                    operation=operation_for_detail,
                    dish_known=True,
                    asana_present=True,
                    detail=detail,
                )
            )
            continue

        title = str(listed.get("name") or "").strip() or (
            None
            if state is None
            else str(state["last_confirmed_title"] or "").strip() or None
        )
        try:
            section_gid = _task_section_gid(listed, COOKING_PROJECT_GID)
        except DishRuleError:
            section_gid = None
        section_name = section_names.get(section_gid or "")

        if not dish_known:
            if section_gid in registry.excluded_gids:
                category = "expected_external_lifecycle"
                reason = "excluded_cooking_section"
                detail = (
                    "This task is in a Cooking section that Dish intentionally does not govern."
                )
            else:
                category = "asana_only"
                reason = "not_recognized_by_dish"
                detail = (
                    "This task is present in Cooking but has no durable Dish workflow/content record."
                )
            items.append(
                _audit_item(
                    task_gid=task_gid,
                    title=title,
                    category=category,
                    reason=reason,
                    section_gid=section_gid,
                    section_name=section_name,
                    operation=None,
                    dish_known=False,
                    asana_present=True,
                    detail=detail,
                )
            )
            continue

        if len(active_operations) > 1:
            items.append(
                _audit_item(
                    task_gid=task_gid,
                    title=title,
                    category="real_inconsistency",
                    reason="multiple_nonterminal_operations",
                    section_gid=section_gid,
                    section_name=section_name,
                    operation=operation_for_detail,
                    dish_known=True,
                    asana_present=True,
                    detail="Dish has multiple non-terminal operations for one task.",
                )
            )
            continue

        active_operation = active_operations[0] if active_operations else None
        if migration_required:
            category = "needs_migration_repair"
            reason = "durable_schema_or_migration_reconciliation"
            detail = (
                "Dish durable state is older than the current supported task schema or "
                "explicitly requires migration reconciliation."
            )
        else:
            category = "healthy_current"
            reason = "current"
            detail = (
                "Dish durable state is internally current. Section, due date, and project "
                "membership are operator-managed and are not audit inconsistencies."
            )
        items.append(
            _audit_item(
                task_gid=task_gid,
                title=title,
                category=category,
                reason=reason,
                section_gid=section_gid,
                section_name=section_name,
                operation=active_operation or latest_operation,
                dish_known=True,
                asana_present=True,
                detail=detail,
            )
        )

    items.sort(
        key=lambda item: (
            _AUDIT_CATEGORY_ORDER.get(str(item["category"]), 99),
            str(item.get("task_title") or item["task_gid"]).casefold(),
            str(item["task_gid"]),
        )
    )
    counts = {name: 0 for name in _AUDIT_CATEGORY_ORDER}
    for item in items:
        counts[str(item["category"])] = counts.get(str(item["category"]), 0) + 1

    return result_envelope(
        command="audit",
        data={
            "project_gid": COOKING_PROJECT_GID,
            "asana_task_count": len(asana_by_gid),
            "dish_known_count": len(known_task_gids),
            "audited_task_count": len(items),
            "category_counts": counts,
            "items": items,
            "ignored_asana_fields": [
                "section",
                "due_date",
                "project_membership",
            ],
        },
    )
