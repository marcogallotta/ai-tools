from __future__ import annotations

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from tests.test_dish_tool_step6_prepare import Backend, PLANNING, release


def test_service_preserves_planning_handoff_start_contract(tmp_path):
    backend = Backend()
    honest = tmp_path / "honest"
    honest.mkdir()
    (honest / "dish-verification-protocol.md").write_text(
        "verification protocol", encoding="utf-8"
    )
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "dish.db",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=lambda role=None: release(honest, role),
    )
    principal = ServicePrincipal(
        owner_id="action",
        run_id="11111111-1111-4111-8111-111111111111",
    )

    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "123456789", "kind": "planning"},
        principal=principal,
        request_id="22222222-2222-4222-8222-222222222222",
    )
    assert started["ok"], started

    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": PLANNING,
        },
        principal=principal,
        request_id="33333333-3333-4333-8333-333333333333",
    )

    assert prepared["ok"], prepared
    assert prepared["data"]["handoff"] == "planning-to-research"
    assert prepared["allowed_actions"] == ["start"]
    assert prepared["data"]["required_start_kind"] == "initial"
    assert prepared["data"]["service_access"] == {"state": "handoff"}
