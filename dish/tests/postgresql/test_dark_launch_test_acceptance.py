from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.support.dark_launch_acceptance import capture_report as _capture_report

from tests.support.postgresql.core import NOW, _bootstrap_registry, _import_one, core_db


pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dish-pg-dark-launch-test-acceptance"


def _namespace() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT))


def test_worker_environment_removes_live_credentials() -> None:
    namespace = _namespace()
    sanitize = namespace["_sanitize_worker_environment"]
    environment = {
        "PATH": "/usr/bin",
        "ASANA_ENV": "/secret/asana.env",
        "ASANA_PAT": "asana-secret",
        "DISH_SERVICE_AGENT_TOKEN": "agent-secret",
        "DISH_SERVICE_ACTION_TOKEN": "action-secret",
        "PROJECTION_ADAPTER_SECRET": "adapter-secret",
        "DISH_PG_DATABASE_URL": "postgresql+psycopg://other-secret",
        "OBSERVABILITY_DSN": "https://dsn-secret",
        "UPSTREAM_API_KEY": "api-key-secret",
        "PGPASSWORD": "pg-secret",
    }

    sanitized, evidence = sanitize(environment, "postgresql+psycopg://test")

    assert sanitized == {
        "PATH": "/usr/bin",
        "DISH_PG_TEST_URL": "postgresql+psycopg://test",
    }
    assert evidence["status"] == "pass"
    assert evidence["asana_credentials_present"] is False
    assert evidence["service_tokens_present"] is False
    assert evidence["projection_adapter_credentials_present"] is False
    assert evidence["removed_variable_names"] == sorted(
        set(environment) - {"PATH"}
    )


def test_path_gate_rejects_resolved_aliases() -> None:
    namespace = _namespace()
    verify = namespace["_verify_distinct_test_paths"]
    test_root = namespace["TEST_STATE_ROOT"]
    sqlite = test_root / "shared.sqlite3"

    with pytest.raises(namespace["AcceptanceError"], match="paths alias"):
        verify(
            {
                "sqlite_authority": sqlite,
                "spool": sqlite,
                "emergency": test_root / "dark-launch-emergency",
                "kill_switch": test_root / "dark-launch.disabled",
            }
        )

    with pytest.raises(namespace["AcceptanceError"], match="alias or overlap"):
        verify(
            {
                "sqlite_authority": sqlite,
                "spool": test_root / "spool.sqlite3",
                "emergency": test_root / "dark-launch-emergency",
                "kill_switch": test_root / "dark-launch-emergency" / "disabled",
            }
        )


def test_path_gate_rejects_production_state() -> None:
    namespace = _namespace()
    verify = namespace["_verify_distinct_test_paths"]
    test_root = namespace["TEST_STATE_ROOT"]

    with pytest.raises(namespace["AcceptanceError"], match="non-TEST spool"):
        verify(
            {
                "sqlite_authority": test_root / "shared.sqlite3",
                "spool": Path("/home/marco/.local/state/dish/prod/spool.sqlite3"),
                "emergency": test_root / "emergency",
                "kill_switch": test_root / "disabled",
            }
        )


def test_service_identity_requires_test_environment_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    service_identity = namespace["_service_identity"]
    output = "\n".join(
        (
            "LoadState=loaded",
            "ActiveState=active",
            "SubState=running",
            "FragmentPath=/etc/systemd/system/dish-service-test.service",
            "EnvironmentFiles=/home/marco/.config/dish-service/prod.env (ignore_errors=no)",
        )
    )
    monkeypatch.setattr(
        namespace["subprocess"],
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=output, stderr=""
        ),
    )

    with pytest.raises(namespace["AcceptanceError"], match="TEST environment file"):
        service_identity()


def test_capture_report_verification_requires_both_surfaces_unchanged() -> None:
    namespace = _namespace()
    verify = namespace["_verify_capture_unchanged"]

    result = verify(_capture_report())

    assert result["status"] == "pass"
    assert result["post_baseline_comparisons"]["private"] == 4
    assert result["post_baseline_comparisons"]["action"] == 6
    assert len(result["baseline_manifest_sha256"]) == 64

    with pytest.raises(namespace["AcceptanceError"], match="observable result changed"):
        verify(_capture_report(mismatch=True))




def test_origin_filter_probe_selects_live_only_and_rolls_back(core_db) -> None:
    from sqlalchemy import func, select

    from dish_pg import stage5_models as tx
    from dish_pg.database import session_scope
    from dish_pg.transition import ProjectionService

    namespace = _namespace()
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        first = _import_one(session, ids, context, asana_gid="1234567801")
        second = _import_one(session, ids, context, asana_gid="1234567802")
        projection = ProjectionService(session)
        epoch = projection.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="origin filter proof",
            created_at=NOW,
            external_effects_enabled=False,
        )
        events = [
            projection._record_event(
                generation_id=context["generation_id"],
                execution_id=None,
                task_id=task.task_id,
                event_type="reproject",
                payload={"task_id": str(task.task_id)},
                source_route="service",
                origin="shadow",
                created_at=NOW,
            )
            for task in (first, second)
        ]
        sources = [namespace["_shadow_event_snapshot"](event) for event in events]
        original_ids = {event.projection_event_id for event in events}
        epoch_id = epoch.projection_epoch_id

    result = namespace["_prove_shadow_origin_filter"](
        factory,
        projection_epoch_id=epoch_id,
        generation_id=context["generation_id"],
        sources=sources,
    )

    assert result["live_origin_selected"] is True
    assert result["shadow_origin_selected"] is False
    assert result["shadow_preceded_live_in_claim_order"] is True
    assert result["same_task_claim_conditions"] is True
    assert result["external_visibility"]["checked_from_separate_transaction"] is False
    assert result["projection_attempt_rows_created"] == 0
    assert result["external_adapter_constructed"] is False
    assert result["asana_mutation_path_invoked"] is False
    assert result["transaction_rolled_back"] is True
    with factory() as session:
        assert session.get(tx.ProjectionEpoch, epoch_id).external_effects_enabled is False
        remaining = set(
            session.scalars(
                select(tx.ProjectionOutboxEvent.projection_event_id).where(
                    tx.ProjectionOutboxEvent.projection_event_id.in_(original_ids)
                )
            )
        )
        count = session.scalar(select(func.count()).select_from(tx.ProjectionOutboxEvent))
    assert remaining == original_ids
    assert count == 2
