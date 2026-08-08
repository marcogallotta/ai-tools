from __future__ import annotations

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from dish_service.openapi import action_openapi
from dish_tool.releases import ResolvedRelease
from tests.support.readiness import _approve_and_submit
from tests.support.verification import make_app


def test_action_schema_exposes_forced_material_reason_contract():
    schema = action_openapi()["components"]["schemas"]["ResultEnvelope"]
    classification = schema["properties"]["data"]["properties"][
        "material_classification"
    ]

    assert "forced_material_reasons" in classification["required"]
    reasons = classification["properties"]["forced_material_reasons"]
    assert reasons["type"] == "array"
    assert reasons["items"] == {"type": "string"}
    assert "empty when no override occurred" in reasons["description"]


def test_service_prepare_preserves_exact_forced_material_reasons(tmp_path):
    app, backend, operation_id, verification_text = make_app(tmp_path)
    _approve_and_submit(app, operation_id, run="initial-review")
    app.conn.close()

    honest = tmp_path / "honest"

    def release(role=None):
        return ResolvedRelease(
            version="1.0.10",
            commit="",
            root=honest,
            protocols=(
                {}
                if role is None
                else {
                    role: (
                        verification_text
                        if role == "verification"
                        else f"{role} protocol"
                    )
                }
            ),
            schema_version="2",
            schema={},
            schema_text="{}",
            migration_metadata={},
            requested_protocol_role=role,
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
        release_loader=release,
    )
    principal = ServicePrincipal(
        owner_id="action",
        run_id="11111111-1111-4111-8111-111111111111",
    )
    started = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "t",
            "kind": "change",
            "change_level": "small",
            "change_reason": "adjust the quantity",
        },
        principal=principal,
        request_id="22222222-2222-4222-8222-222222222222",
    )
    assert started["ok"], started

    candidate = f"{backend.title}\n{backend.notes}".replace(
        "100 g test ingredient", "110 g test ingredient"
    )
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": candidate,
            "material_classification": "non-material",
        },
        principal=principal,
        request_id="33333333-3333-4333-8333-333333333333",
    )

    assert prepared["ok"], prepared
    assert prepared["data"]["material_classification"] == {
        "classified_subject": "canonical body diff from the signed baseline",
        "requested": "non-material",
        "effective": "material",
        "forced_material_reasons": ["quantities", "quantity", "portions"],
        "route": "verification",
    }
