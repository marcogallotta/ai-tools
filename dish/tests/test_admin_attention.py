from __future__ import annotations

import pytest

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.database_initialization import initialize_database
from tests.support.abandonment import Backend, _source
from tests.support.abandonment_admin import _released_actor_lease


@pytest.mark.smoke
def test_attention_lists_dead_released_attempt_without_mutating_workflow():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    lease = _released_actor_lease(conn, source["operation_id"])
    before = conn.total_changes
    backend.forbid("read_task", "fleet attention must not live-read tasks")

    result = DishAdminApplication(conn, backend=backend).execute("attention")

    assert result["ok"]
    assert result["data"]["read_only"] is True
    assert result["data"]["checked_count"] == 1
    assert result["data"]["live_inspection_count"] == 0
    assert result["data"]["attention_count"] == 1
    assert result["data"]["needs_you_count"] == 0
    assert result["data"]["category_counts"]["system"] == 1
    item = result["data"]["attention_items"][0]
    assert item["operation_id"] == source["operation_id"]
    assert item["category"] == "system"
    assert item["needs_you"] is False
    assert [signal["kind"] for signal in item["signals"]] == ["inactive_run"]
    # The command writes only its invocation audit; workflow rows are unchanged.
    assert conn.execute(
        "SELECT status,phase FROM operations WHERE operation_id=?",
        (source["operation_id"],),
    ).fetchone()[:] == ("open", "prepare_required")
    assert conn.execute("SELECT COUNT(*) FROM abandonment_attempts").fetchone()[0] == 0
    assert conn.total_changes > before


@pytest.mark.smoke
def test_attention_treats_an_unexpired_active_actor_lease_as_healthy():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning", run_id="live-run")
    LeaseManager(conn).acquire(
        source["operation_id"], ServicePrincipal("owner", "live-run")
    )

    result = DishAdminApplication(conn, backend=backend).execute("attention")

    assert result["ok"]
    assert result["data"]["checked_count"] == 1
    assert result["data"]["live_inspection_count"] == 0
    assert result["data"]["healthy_count"] == 1
    assert result["data"]["attention_count"] == 0
    assert result["data"]["attention_items"] == []

def _terminalize(conn, operation_id: str) -> None:
    conn.execute(
        "UPDATE operations SET status='completed', phase='terminal', "
        "completed_at='2026-08-04T00:00:00Z', terminal_outcome='submitted' "
        "WHERE operation_id=?",
        (operation_id,),
    )


@pytest.mark.smoke
def test_attention_hides_clean_completed_task_without_abnormal_durable_state():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning", run_id="completed-run")
    _terminalize(conn, source["operation_id"])
    backend.forbid("read_task", "fleet attention must not live-read terminal tasks")

    result = DishAdminApplication(conn, backend=backend).execute("attention")

    assert result["ok"]
    assert result["data"]["live_inspection_count"] == 0
    assert result["data"]["attention_count"] == 0
    assert result["data"]["attention_items"] == []


@pytest.mark.smoke
def test_attention_surfaces_unreleased_actor_lease_on_terminal_operation():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning", run_id="completed-run")
    LeaseManager(conn).acquire(
        source["operation_id"], ServicePrincipal("owner", "completed-run")
    )
    _terminalize(conn, source["operation_id"])
    backend.forbid("read_task", "fleet attention must not live-read terminal tasks")

    result = DishAdminApplication(conn, backend=backend).execute("attention")

    assert result["data"]["active_dish_count"] == 0
    assert result["data"]["healthy_count"] == 0
    assert result["data"]["attention_count"] == 1
    assert result["data"]["needs_you_count"] == 1
    item = result["data"]["attention_items"][0]
    assert item["operation_id"] == source["operation_id"]
    assert item["category"] == "unsafe"
    assert [signal["kind"] for signal in item["signals"]] == ["terminal_lease_open"]


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("durable_state", "expected_kind"),
    [
        ("execution", "unresolved_execution"),
        ("write", "unresolved_write"),
        ("movement", "unresolved_movement"),
    ],
)
def test_attention_surfaces_unresolved_effect_state_on_terminal_operation(
    durable_state, expected_kind
):
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning", run_id="completed-run")
    operation_id = source["operation_id"]
    if durable_state == "execution":
        conn.execute(
            """INSERT INTO operation_executions(
                   execution_id,operation_id,request_id,command,baseline_json,status,
                   evidence_json,created_at,completed_at
               ) VALUES('execution',?,NULL,'prepare','{}','started',NULL,
                        '2026-08-01T00:00:00Z',NULL)""",
            (operation_id,),
        )
    elif durable_state == "write":
        conn.execute(
            """INSERT INTO write_attempts(
                   attempt_id,operation_id,expected_identity,intended_identity,outcome,
                   started_at,purpose
               ) VALUES('write',?,?,NULL,'started','2026-08-01T00:00:00Z','content_write')""",
            (operation_id, source["expected_identity"]),
        )
    else:
        conn.execute(
            """INSERT INTO movement_attempts(
                   attempt_id,operation_id,expected_section_gid,intended_section_gid,
                   outcome,started_at,purpose
               ) VALUES('movement',?,'pi','verification','started',
                        '2026-08-01T00:00:00Z','verification_handoff')""",
            (operation_id,),
        )
    _terminalize(conn, operation_id)
    backend.forbid("read_task", "fleet attention must not live-read terminal tasks")

    result = DishAdminApplication(conn, backend=backend).execute("attention")

    assert result["data"]["active_dish_count"] == 0
    assert result["data"]["attention_count"] == 1
    assert result["data"]["needs_you_count"] == 1
    item = result["data"]["attention_items"][0]
    assert item["operation_id"] == operation_id
    assert item["category"] == "unsafe"
    assert [signal["kind"] for signal in item["signals"]] == [expected_kind]


@pytest.mark.smoke
def test_attention_surfaces_incomplete_abandonment_on_terminal_source_operation():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning", run_id="dead-run")
    from tests.support.abandonment import _abandon

    _abandon(conn, source)
    _terminalize(conn, source["operation_id"])
    backend.forbid("read_task", "fleet attention must not live-read terminal tasks")

    result = DishAdminApplication(conn, backend=backend).execute("attention")

    assert result["data"]["active_dish_count"] == 0
    assert result["data"]["attention_count"] == 1
    assert result["data"]["needs_you_count"] == 1
    item = result["data"]["attention_items"][0]
    assert item["operation_id"] == source["operation_id"]
    assert item["category"] == "needs_marco"
    assert [signal["kind"] for signal in item["signals"]] == ["abandonment_recovery"]


@pytest.mark.smoke
def test_issues_is_primary_and_attention_alias_keeps_compatibility():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning")
    _released_actor_lease(conn, source["operation_id"])

    primary = DishAdminApplication(conn, backend=backend).execute("issues")
    alias = DishAdminApplication(conn, backend=backend).execute("attention")

    assert primary["command"] == "issues"
    assert primary["data"]["issue_count"] == 1
    assert primary["data"]["issue_items"][0]["operation_id"] == source["operation_id"]
    assert alias["command"] == "attention"
    assert alias["data"]["attention_items"] == primary["data"]["issue_items"]


@pytest.mark.smoke
def test_issues_treats_expired_open_lease_as_system_recoverable_not_marco_required():
    from datetime import datetime, timezone

    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning", run_id="expired-run")
    LeaseManager(
        conn, now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    ).acquire(source["operation_id"], ServicePrincipal("owner", "expired-run"))

    result = DishAdminApplication(conn, backend=backend).execute("issues")

    assert result["data"]["needs_you_count"] == 0
    assert result["data"]["system_count"] == 1
    item = result["data"]["issue_items"][0]
    assert item["category"] == "system"
    assert item["needs_you"] is False
    assert [signal["kind"] for signal in item["signals"]] == ["expired_lease"]


@pytest.mark.smoke
def test_active_leases_lists_unreleased_actor_leases_without_backend_reads():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning", run_id="live-run")
    LeaseManager(conn).acquire(
        source["operation_id"], ServicePrincipal("owner", "live-run")
    )
    backend.forbid("read_task", "active-leases must be durable-state only")

    result = DishAdminApplication(conn, backend=backend).execute("active-leases")

    assert result["ok"]
    assert result["data"]["read_only"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["state_counts"]["active"] == 1
    lease = result["data"]["leases"][0]
    assert lease["operation_id"] == source["operation_id"]
    assert lease["run_id"] == "live-run"
    assert lease["authority_state"] == "active"


@pytest.mark.smoke
def test_active_leases_marks_unreleased_expired_lease():
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning", run_id="expired-run")
    from datetime import datetime, timezone
    LeaseManager(
        conn, now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    ).acquire(source["operation_id"], ServicePrincipal("owner", "expired-run"))

    result = DishAdminApplication(conn, backend=backend).execute("active-leases")

    assert result["data"]["state_counts"]["expired"] == 1
    assert result["data"]["leases"][0]["authority_state"] == "expired"


@pytest.mark.smoke
def test_attention_counts_pending_human_review_as_needs_you(monkeypatch):
    conn = initialize_database(":memory:")
    backend = Backend(section="pi")
    source = _source(conn, backend, kind="planning", run_id="review-run")

    def fake_review_items(conn, *, proposal_statuses, include_human_holds):
        assert proposal_statuses == ("pending",)
        return ({
            "operation_id": source["operation_id"],
            "review_id": "review-1",
            "item_type": "human_review",
            "proposal_reason": "Choose whether this remains a main dish.",
            "review_summary": {"issue": "Protein target requires Marco's choice."},
        },)

    monkeypatch.setattr("dish_tool.admin.list_review_items", fake_review_items)
    result = DishAdminApplication(conn, backend=backend).execute("attention")

    assert result["data"]["needs_you_count"] == 1
    item = result["data"]["attention_items"][0]
    assert item["category"] == "needs_marco"
    assert item["signals"][0]["review_id"] == "review-1"
    assert item["signals"][0]["shell_command"] == "dish-admin review-inspect review-1"


def test_review_queue_active_filter_means_waiting_for_marco(monkeypatch):
    conn = initialize_database(":memory:")
    captured = {}

    def fake_review_items(conn, *, proposal_statuses, include_human_holds):
        captured["statuses"] = proposal_statuses
        captured["holds"] = include_human_holds
        return ()

    monkeypatch.setattr("dish_tool.admin.list_review_items", fake_review_items)
    result = DishAdminApplication(conn).execute("review-queue")

    assert result["ok"]
    assert captured == {"statuses": ("pending",), "holds": True}

@pytest.mark.smoke
def test_inspect_known_dish_remains_available_after_operator_moves_task_outside_cooking():
    from datetime import datetime, timezone

    from dish_tool.database import confirm_task_content, create_operation
    from dish_tool.identifiers import stable_dish_uuid_for_asana_identity
    from dish_tool.models import OperationActors
    from tests.support.asana_backend import StatefulAsanaBackend

    task_gid = "1217333270126271"

    class OutsideBackend(StatefulAsanaBackend):
        def read_task(self, gid: str):
            row = super().read_task(gid)
            row["projects"] = [{"gid": "operator-managed-project"}]
            row["memberships"] = [
                {
                    "project": {"gid": "operator-managed-project"},
                    "section": {"gid": "operator-managed-section"},
                }
            ]
            return row

    conn = initialize_database(":memory:")
    backend = OutsideBackend(task_gid=task_gid, title="Moved dish", notes="notes", section="rq")
    baseline = confirm_task_content(
        conn, task_gid=task_gid, title=backend.title, notes=backend.notes,
        schema_version="2", boundary="test",
    )
    operation = create_operation(
        conn, task_gid=task_gid, operation_kind="planning",
        expected_identity=baseline.digest, schema_version="2", expected_section_gid="rq",
        actors=OperationActors(editor_agent="gpt", run_id="expired-run"),
    )
    LeaseManager(
        conn, now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    ).acquire(operation["operation_id"], ServicePrincipal("owner", "expired-run"))
    dish_id = str(stable_dish_uuid_for_asana_identity("task", task_gid))

    result = DishAdminApplication(conn, backend=backend).execute("inspect", dish=dish_id)

    assert result["ok"] is True
    assert result["data"]["task_title"] == backend.title
    assert result["data"]["operation_id"] == operation["operation_id"]
    assert "remains inspectable" in result["data"]["problem"]
    assert result["data"]["human_actions"] == []
