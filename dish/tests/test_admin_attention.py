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

    result = DishAdminApplication(conn, backend=backend).execute("attention")

    assert result["ok"]
    assert result["data"]["read_only"] is True
    assert result["data"]["checked_count"] == 1
    assert result["data"]["attention_count"] == 1
    assert result["data"]["category_counts"]["multi_step_safe"] == 1
    item = result["data"]["attention_items"][0]
    assert item["operation_id"] == source["operation_id"]
    assert item["category"] == "multi_step_safe"
    assert item["human_actions"][0]["kind"] == "abandon-dead-agent"
    assert lease["lease_id"] in item["human_actions"][0]["shell_command"]
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
    assert result["data"]["healthy_count"] == 1
    assert result["data"]["attention_count"] == 0
    assert result["data"]["attention_items"] == []
