from __future__ import annotations

from pathlib import Path

from dish_tool.admin import DishAdminApplication
from dish_tool.database import confirm_task_content, create_operation, transition_operation
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import ResolvedRelease
from dish_tool.admin_human import render_admin_result
from tests.support.asana_backend import StatefulAsanaBackend


class AuditBackend(StatefulAsanaBackend):
    def __init__(self, *args, outside_project: set[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.outside_project = set(outside_project or ())

    def list_tasks_for_project(self, project_gid: str, *, cursor: str | None = None):
        arguments = {"project_gid": project_gid, "cursor": cursor}

        def effect():
            if project_gid != self.project_gid:
                raise AssertionError(f"unexpected project gid: {project_gid}")
            matches = [
                self._task_response(gid)
                for gid in self.tasks
                if gid not in self.outside_project
            ]
            start = int(cursor) if cursor else 0
            page = matches[start : start + self.section_tasks_page_size]
            end = start + len(page)
            next_cursor = str(end) if end < len(matches) else None
            return page, next_cursor

        return self._invoke("list_tasks_for_project", arguments, effect)

    def read_task(self, gid: str):
        if gid not in self.tasks:
            raise DishRuleError("NOT_FOUND", f"task not found: {gid}", rule="task_not_found")
        row = super().read_task(gid)
        if gid in self.outside_project:
            row["projects"] = [{"gid": "other-project"}]
            row["memberships"] = [
                {"project": {"gid": "other-project"}, "section": {"gid": "history"}}
            ]
        return row


def _release():
    return ResolvedRelease(
        version="1.0.11",
        commit="test",
        root=Path("."),
        protocols={},
        schema_version="2",
        schema={},
        schema_text="{}",
    )


def _known(conn, task_gid: str, *, schema_version: str = "2"):
    return confirm_task_content(
        conn,
        task_gid=task_gid,
        title=f"Dish {task_gid}",
        notes="notes",
        schema_version=schema_version,
        boundary="test",
    )


def _app(tmp_path, backend: AuditBackend):
    conn = initialize_database(tmp_path / "dish.db")
    return conn, DishAdminApplication(conn, backend=backend, release_loader=_release)


def _by_gid(result):
    return {row["task_gid"]: row for row in result["data"]["items"]}


def test_population_audit_classifies_current_manual_asana_only_missing_and_migration(tmp_path):
    backend = AuditBackend(
        tasks=[
            {"gid": "1001", "title": "Healthy", "notes": "notes", "section_gid": "rq"},
            {"gid": "1002", "title": "Moved manually", "notes": "notes", "section_gid": "vq"},
            {"gid": "1003", "title": "Asana only", "notes": "", "section_gid": "rq"},
            {"gid": "1004", "title": "Reference", "notes": "", "section_gid": "ref"},
            {"gid": "1005", "title": "Old schema", "notes": "notes", "section_gid": "rq"},
            {"gid": "1007", "title": "Active mismatch", "notes": "notes", "section_gid": "vq"},
            {"gid": "1008", "title": "Archived", "notes": "notes", "section_gid": "rq"},
        ],
        outside_project={"1008"},
    )
    conn, app = _app(tmp_path, backend)

    healthy = _known(conn, "1001")
    moved = _known(conn, "1002")
    moved_op = create_operation(
        conn,
        task_gid="1002",
        operation_kind="initial",
        expected_identity=moved.digest,
        schema_version="2",
        expected_section_gid="rq",
    )
    transition_operation(
        conn,
        moved_op["operation_id"],
        phase="terminal",
        status="cancelled",
        terminal_outcome="cancelled_by_marco",
    )
    _known(conn, "1005", schema_version="1")
    _known(conn, "1006")  # durable only; intentionally absent from Asana
    mismatch = _known(conn, "1007")
    create_operation(
        conn,
        task_gid="1007",
        operation_kind="initial",
        expected_identity=mismatch.digest,
        schema_version="2",
        expected_section_gid="rq",
    )
    _known(conn, "1008")

    result = app.execute("audit")
    assert result["ok"] is True
    rows = _by_gid(result)

    assert rows["1001"]["category"] == "healthy_current"
    assert rows["1002"]["category"] == "healthy_current"
    assert rows["1002"]["reason"] == "current"
    assert rows["1003"]["category"] == "asana_only"
    assert rows["1004"]["category"] == "expected_external_lifecycle"
    assert rows["1004"]["reason"] == "excluded_cooking_section"
    assert rows["1005"]["category"] == "needs_migration_repair"
    assert rows["1006"]["category"] == "dish_known_asana_missing_or_unavailable"
    assert rows["1006"]["reason"] == "asana_task_missing"
    assert rows["1007"]["category"] == "healthy_current"
    assert rows["1007"]["reason"] == "current"
    assert rows["1008"]["category"] == "healthy_current"
    assert rows["1008"]["reason"] == "operator_managed_asana_organization"

    counts = result["data"]["category_counts"]
    assert counts == {
        "real_inconsistency": 0,
        "needs_migration_repair": 1,
        "dish_known_asana_missing_or_unavailable": 1,
        "asana_only": 1,
        "expected_external_lifecycle": 1,
        "healthy_current": 4,
    }


def test_population_audit_ignores_active_task_project_membership(tmp_path):
    backend = AuditBackend(
        tasks=[{"gid": "2001", "title": "Active outside", "notes": "notes", "section_gid": "rq"}],
        outside_project={"2001"},
    )
    conn, app = _app(tmp_path, backend)
    identity = _known(conn, "2001")
    create_operation(
        conn,
        task_gid="2001",
        operation_kind="initial",
        expected_identity=identity.digest,
        schema_version="2",
        expected_section_gid="rq",
    )

    row = _by_gid(app.execute("audit"))["2001"]
    assert row["category"] == "healthy_current"
    assert row["reason"] == "operator_managed_asana_organization"


def test_population_audit_ignores_active_section_mismatch(tmp_path):
    backend = AuditBackend(
        tasks=[{"gid": "2002", "title": "Moved by Marco", "notes": "notes", "section_gid": "vq"}],
    )
    conn, app = _app(tmp_path, backend)
    identity = _known(conn, "2002")
    create_operation(
        conn,
        task_gid="2002",
        operation_kind="initial",
        expected_identity=identity.digest,
        schema_version="2",
        expected_section_gid="rq",
    )

    row = _by_gid(app.execute("audit"))["2002"]
    assert row["category"] == "healthy_current"
    assert row["reason"] == "current"



def test_population_audit_paginates_project_listing_without_per_task_reads(tmp_path):
    backend = AuditBackend(
        tasks=[
            {"gid": str(3000 + i), "title": f"Task {i}", "notes": "", "section_gid": "rq"}
            for i in range(5)
        ]
    )
    backend.section_tasks_page_size = 2
    conn, app = _app(tmp_path, backend)

    result = app.execute("audit")
    assert result["ok"] is True
    assert result["data"]["asana_task_count"] == 5
    assert result["data"]["category_counts"]["asana_only"] == 5
    assert len(backend.calls("list_tasks_for_project")) == 3
    assert backend.calls("read_task") == []


def test_population_audit_human_output_hides_healthy_rows_unless_verbose(tmp_path):
    backend = AuditBackend(
        tasks=[
            {"gid": "4001", "title": "Healthy", "notes": "notes", "section_gid": "rq"},
            {"gid": "4002", "title": "Unknown", "notes": "", "section_gid": "rq"},
        ]
    )
    conn, app = _app(tmp_path, backend)
    _known(conn, "4001")
    result = app.execute("audit")

    compact = render_admin_result(result, profile="test")
    assert "Cooking population audit" in compact
    assert "[ASANA ONLY] Unknown" in compact
    assert "[HEALTHY] Healthy" not in compact
    assert "Healthy/current rows are hidden" in compact

    verbose = render_admin_result(result, profile="test", verbose=True)
    assert "[HEALTHY] Healthy" in verbose
    assert "Rule: current" in verbose
