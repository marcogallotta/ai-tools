from __future__ import annotations

import json
from datetime import timedelta

from dish_pg import stage5_models as tx
from dish_pg.dark_launch import StatusThresholds, status
from dish_pg.database import session_scope
from dish_pg.transition import ShadowService
from dish_service.path_safety import engage_kill_switch
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.core import HASH_A
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def test_dark_launch_status_reports_bounded_operational_health(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id
    spool = ShadowSpool(tmp_path / "spool.sqlite3")
    spool.status()
    report = status(
        session_maker=factory,
        spool=ShadowSpool.open_existing_read_only(tmp_path / "spool.sqlite3"),
        baseline_id=baseline_id,
        kill_switch_path=tmp_path / "dark-launch.disabled",
        worker_unit={
            "load_state": "loaded",
            "active_state": "active",
            "sub_state": "running",
            "unit_file_state": "enabled",
            "result": "success",
        },
        thresholds=StatusThresholds(
            warning_backlog=10,
            critical_backlog=20,
            warning_lag_seconds=30,
            critical_lag_seconds=60,
            warning_capacity_percent=70,
            critical_capacity_percent=90,
            warning_mismatches=1,
            critical_mismatches=2,
            warning_gaps=1,
            critical_gaps=2,
        ),
        observed_at=NOW,
    )
    assert report["observed_at"] == NOW.isoformat()
    assert report["baseline"]["shadow_baseline_id"] == str(baseline_id)
    assert report["baseline"]["delivery_counts"]["pending"] == 0
    assert report["spool"]["counts"]["complete"] == 0
    assert report["spool"]["backlog"] == 0
    assert report["spool"]["oldest_pending_age_seconds"] is None
    assert report["kill_switch"]["state"] == "clear"
    assert report["health"]["dimensions"]["backlog"]["state"] == "healthy"
    assert report["health"]["dimensions"]["worker_unit"]["state"] == "healthy"
    assert report["health"]["state"] == "healthy"
    assert len(json.dumps(report, sort_keys=True)) < 20_000


def test_status_calculates_lag_and_thresholds_without_mutation(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id
    spool = ShadowSpool(tmp_path / "spool.sqlite3")
    spool.reserve(
        registration_id="registration-1",
        source_request_identity="request-1",
        source_authority_generation="legacy-1",
        command_name="start",
        treatment="execute",
        canonical_input={"task_gid": "1"},
        principal={"kind": "agent"},
        source_pre_state={"selected_tables": [], "tables": {}},
        pinned_inputs={"rollout_mode": "execute"},
        created_at=NOW - timedelta(seconds=75),
    )
    report = status(
        session_maker=factory,
        spool=ShadowSpool.open_existing_read_only(
            tmp_path / "spool.sqlite3",
            max_records=1,
            max_bytes=1_000_000_000,
            min_free_bytes=1,
        ),
        baseline_id=baseline_id,
        kill_switch_path=tmp_path / "dark-launch.disabled",
        thresholds=StatusThresholds(
            warning_backlog=1,
            critical_backlog=5,
            warning_lag_seconds=30,
            critical_lag_seconds=60,
            warning_capacity_percent=50,
            critical_capacity_percent=90,
            warning_mismatches=1,
            critical_mismatches=2,
            warning_gaps=1,
            critical_gaps=2,
        ),
        observed_at=NOW,
    )
    assert report["spool"]["oldest_pending_age_seconds"] == 75.0
    assert report["health"]["dimensions"]["backlog"]["state"] == "warning"
    assert report["health"]["dimensions"]["lag"]["state"] == "critical"
    assert report["health"]["dimensions"]["capacity"]["state"] == "critical"
    assert report["health"]["state"] == "critical"
    persisted = ShadowSpool.open_existing_read_only(tmp_path / "spool.sqlite3").status()
    assert persisted["counts"]["reserved"] == 1


def test_status_reports_distinct_parity_and_open_gap_thresholds(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id
        for index, parity in enumerate(("gap", "mismatch"), 1):
            envelope_id = _next(ids)
            session.add(
                tx.ShadowEnvelope(
                    envelope_id=envelope_id,
                    shadow_baseline_id=baseline_id,
                    command_name="start",
                    source_request_identity=f"request-{index}",
                    canonical_input={"task_gid": str(index)},
                    canonical_input_sha256=HASH_A,
                    source_outcome={"status": "ok"},
                    source_outcome_sha256=HASH_A,
                    source_post_state={},
                    rollout_sequence=index,
                    source_authority_generation="legacy-1",
                    source_execution_identity=f"execution-{index}",
                    principal={"kind": "agent"},
                    source_pre_state={},
                    source_pre_state_sha256=HASH_A,
                    pinned_inputs={},
                    source_effects={},
                    capture_qualification="execute",
                    source_post_state_sha256=HASH_A,
                    envelope_schema_version=1,
                    captured_at=NOW,
                )
            )
            session.flush()
            session.add(
                tx.ShadowComparison(
                    comparison_id=_next(ids),
                    envelope_id=envelope_id,
                    target_result={},
                    target_result_sha256=HASH_A,
                    parity_class=parity,
                    differences=[],
                    comparator_release="fixture",
                    compared_at=NOW,
                )
            )
            session.add(
                tx.ShadowGap(
                    gap_id=_next(ids),
                    shadow_baseline_id=baseline_id,
                    envelope_id=envelope_id,
                    gap_identity=f"{parity}:request-{index}",
                    gap_kind="uncomparable" if parity == "gap" else "mismatch",
                    state="open",
                    details={},
                    resolution=None,
                    gap_revision=1,
                    created_at=NOW,
                    resolved_at=None,
                )
            )
    spool_path = tmp_path / "spool.sqlite3"
    ShadowSpool(spool_path).status()
    report = status(
        session_maker=factory,
        spool=ShadowSpool.open_existing_read_only(spool_path),
        baseline_id=baseline_id,
        kill_switch_path=tmp_path / "dark-launch.disabled",
        thresholds=StatusThresholds(
            warning_backlog=10,
            critical_backlog=20,
            warning_lag_seconds=30,
            critical_lag_seconds=60,
            warning_capacity_percent=70,
            critical_capacity_percent=90,
            warning_mismatches=1,
            critical_mismatches=2,
            warning_gaps=2,
            critical_gaps=3,
        ),
        observed_at=NOW,
    )
    assert report["baseline"]["parity_counts"]["gap"] == 1
    assert report["baseline"]["parity_counts"]["mismatch"] == 1
    assert report["baseline"]["open_gaps"] == 2
    assert report["health"]["dimensions"]["mismatches"]["state"] == "warning"
    assert report["health"]["dimensions"]["gaps"]["value"] == 2
    assert report["health"]["dimensions"]["gaps"]["state"] == "warning"


def test_status_classifies_kill_switch_and_absent_systemd(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
    spool_path = tmp_path / "spool.sqlite3"
    ShadowSpool(spool_path).status()
    marker = tmp_path / "dark-launch.disabled"
    engage_kill_switch(marker, {"reason": "fixture"})
    report = status(
        session_maker=factory,
        spool=ShadowSpool.open_existing_read_only(spool_path),
        baseline_id=baseline.shadow_baseline_id,
        kill_switch_path=marker,
        thresholds=StatusThresholds(),
        observed_at=NOW,
    )
    assert report["kill_switch"]["state"] == "engaged"
    assert report["health"]["dimensions"]["kill_switch"]["state"] == "critical"
    assert report["health"]["dimensions"]["worker_unit"]["state"] == "unavailable"


def test_status_reports_unavailable_worker_without_parsing_free_text(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
    spool_path = tmp_path / "spool.sqlite3"
    ShadowSpool(spool_path).status()
    report = status(
        session_maker=factory,
        spool=ShadowSpool.open_existing_read_only(spool_path),
        baseline_id=baseline.shadow_baseline_id,
        kill_switch_path=tmp_path / "dark-launch.disabled",
        worker_unit={
            "state": "unavailable",
            "reason": "systemctl is unavailable",
            "unit_name": "dish-shadow-worker.service",
        },
        thresholds=StatusThresholds(
            warning_backlog=10,
            critical_backlog=20,
            warning_lag_seconds=30,
            critical_lag_seconds=60,
            warning_capacity_percent=70,
            critical_capacity_percent=90,
            warning_mismatches=1,
            critical_mismatches=2,
            warning_gaps=1,
            critical_gaps=2,
        ),
        observed_at=NOW,
    )
    assert report["health"]["dimensions"]["worker_unit"]["state"] == "unavailable"
    assert report["health"]["state"] == "unavailable"


def test_status_reports_database_unavailable_as_structured_health(tmp_path):
    spool_path = tmp_path / "spool.sqlite3"
    ShadowSpool(spool_path).status()

    def unavailable_factory():
        raise RuntimeError(
            "cannot connect to postgresql+psycopg://dish:secret-value@localhost/dish_prod"
        )

    report = status(
        session_maker=unavailable_factory,
        spool=ShadowSpool.open_existing_read_only(spool_path),
        baseline_id=None,
        kill_switch_path=tmp_path / "dark-launch.disabled",
        thresholds=StatusThresholds(
            warning_backlog=10,
            critical_backlog=20,
            warning_lag_seconds=30,
            critical_lag_seconds=60,
            warning_capacity_percent=70,
            critical_capacity_percent=90,
            warning_mismatches=1,
            critical_mismatches=2,
            warning_gaps=1,
            critical_gaps=2,
        ),
        observed_at=NOW,
    )
    assert report["postgresql"]["state"] == "unavailable"
    assert "secret-value" not in report["postgresql"]["reason"]
    assert report["health"]["dimensions"]["mismatches"]["state"] == "unavailable"
    assert report["health"]["dimensions"]["gaps"]["state"] == "unavailable"
    assert report["health"]["state"] == "unavailable"
    assert len(json.dumps(report, sort_keys=True)) < 20_000
