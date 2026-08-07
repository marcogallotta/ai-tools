from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from dish_pg import models as core_models
from dish_pg import stage5_models as projection_models
from dish_pg.database import session_scope
from dish_pg.production_shaped_rehearsal import (
    MANIFEST_SCHEMA,
    PHASES,
    REPORT_SCHEMA,
    SOURCE_IDENTITY_PATHS,
    PhaseRecorder,
    ProductionShapedError,
    SafeRunner,
    _describe_input_identities,
    _checkout_identity,
    _cleanup_owned_resources,
    _cluster_cleanup_evidence,
    _deployment_configuration_identity,
    _import_projection_fault_task,
    _load_corpus_manifest,
    _report_hash,
    _validate_manifest_bindings,
    _safe_child_env,
    _runtime_entrypoint_identity,
    _source_manifest,
    _validate_isolation_inputs,
    main,
    run,
)
from dish_pg.postgres_service import _section4_control_point
from dish_pg.production_shaped_runtime import BarrierServer, ServiceRuntimeClient, reach_barrier
from dish_pg.production_shaped_support import (
    LocalProjectionAdapter,
    _atomic_json as support_atomic_json,
    _owned_evidence_path,
    corpus_identity,
    fetch_sanitized_corpus,
    parse_corpus_identity,
)
from dish_pg.recovery_rehearsal import RehearsalBlocked, Runner
from dish_pg.transition import ProjectionClaim, ProjectionService
from dish_pg.workflow import sha256_json
from tests.support.postgresql.workflow import NOW, workflow_db


def _record(tmp_path: Path) -> dict[str, object]:
    return {
        "task_id": "77777777-7777-4777-8777-777777777777",
        "asana_task_gid": "9000000000000001",
        "title": "Sanitized production-shaped task",
        "body": "Synthetic text with realistic length and no production locator.",
        "identity_scheme": "sanitized-v1",
        "content_identity": "sanitized-content-1",
        "project_ids": ["1ae6e7ba-31e3-5dc5-9565-4ea37b49ac97"],
        "section_id": "8b5bfb31-b986-5116-a207-569a5ba95907",
        "completed": False,
        "observed_at": "2026-08-06T12:00:00Z",
    }


def _corpus_and_manifest(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    corpus = tmp_path / "sanitized.ndjson"
    corpus.write_text(json.dumps(_record(tmp_path), sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(corpus.read_bytes()).hexdigest()
    value: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "sanitized": True,
        "resource_scope": "local_or_test_only",
        "production_contact_prohibited": True,
        "contains_production_credentials": False,
        "corpus_sha256": digest,
        "record_count": 1,
        "deployment_identity": {"identity": "deployment-test"},
        "source_manifest": {"identity": "source-test"},
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return corpus, manifest, value


def test_section4_phase_order_is_complete_and_enforced():
    assert PHASES == (
        "postgresql_migration",
        "corpus_import",
        "reconciliation",
        "service_and_worker_startup",
        "representative_commands",
        "process_and_database_fault_injection",
        "physical_backup",
        "independent_restore",
        "point_in_time_recovery",
        "final_reconciliation_and_evidence",
    )
    recorder = PhaseRecorder()
    with pytest.raises(ProductionShapedError, match="phase order violation"):
        recorder.run("corpus_import", lambda: {})
    for phase in PHASES:
        recorder.run(phase, lambda phase=phase: {"phase": phase})
    assert [item.name for item in recorder.items] == list(PHASES)
    assert all(item.first_attempt_status == "passed" for item in recorder.items)




def test_local_projection_output_requires_owned_evidence_and_mode_0600(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    marker = evidence / ".dish-section4-evidence"
    marker.write_text(json.dumps({"schema": REPORT_SCHEMA}) + "\n", encoding="utf-8")
    output = evidence / "projection.json"
    assert _owned_evidence_path(output) == output.resolve()
    support_atomic_json(output, {"ok": True})
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="not beneath owned"):
        _owned_evidence_path(tmp_path / "outside.json")


def _local_projection_claim(tmp_path: Path) -> tuple[ProjectionClaim, Path, dict[str, object]]:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / ".dish-section4-evidence").write_text(
        json.dumps({"schema": REPORT_SCHEMA}) + "\n", encoding="utf-8"
    )
    output = evidence / "projection.json"
    projected_state: dict[str, object] = {
        "task_id": "77777777-7777-4777-8777-777777777777",
        "title": "Canonical projected state",
        "body": "Exact local-only external state.",
        "project_ids": ["1ae6e7ba-31e3-5dc5-9565-4ea37b49ac97"],
        "section_id": "8b5bfb31-b986-5116-a207-569a5ba95907",
        "completed": False,
    }
    claim = ProjectionClaim(
        event_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        claim_revision=2,
        claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        task_id=uuid.UUID(str(projected_state["task_id"])),
        aggregate_sequence=1,
        event_type="reproject",
        payload={
            "local_store_path": str(output),
            "authoritative_snapshot": projected_state,
        },
        idempotency_key="a" * 64,
    )
    return claim, output, projected_state


def test_local_projection_adapter_emits_reproject_external_observation_and_reuses_it_for_recovery(
    tmp_path,
):
    claim, output, projected_state = _local_projection_claim(tmp_path)
    adapter = LocalProjectionAdapter()
    attempt = adapter.prepare(claim)
    observation = adapter.attempt_and_observe(claim, attempt)
    expected_identity = sha256_json(projected_state)
    fact = observation.evidence["external_observation"]
    projected = json.loads(output.read_text(encoding="utf-8"))
    assert projected["event_type"] == claim.event_type
    assert projected["projected_state"] == projected_state
    assert observation.observed_applied is True
    assert observation.observed_identity == expected_identity
    assert fact == {
        "source": "external_reread",
        "operation": "reproject",
        "observed_external_id": f"local-task:{claim.task_id}",
        "observed_reproject_state_identity": expected_identity,
    }

    recovery = adapter.observe_recovery(claim, attempt)
    assert recovery.observed_applied is True
    assert recovery.observed_identity == expected_identity
    assert recovery.evidence["external_observation"] == fact


def test_local_projection_adapter_missing_store_proves_external_absence(tmp_path):
    claim, _output, _projected_state = _local_projection_claim(tmp_path)
    observation = LocalProjectionAdapter().observe_recovery(
        claim, LocalProjectionAdapter().prepare(claim)
    )
    assert observation.observed_applied is False
    assert observation.observed_identity is None
    assert observation.reread_complete is True
    assert observation.evidence["external_observation"] == {
        "source": "external_reread",
        "operation": "reproject",
        "observed_external_id": f"local-task:{claim.task_id}",
        "observed_absent": True,
    }


def test_local_projection_adapter_malformed_reread_stays_uncertain(tmp_path):
    claim, output, _projected_state = _local_projection_claim(tmp_path)
    adapter = LocalProjectionAdapter()
    attempt = adapter.prepare(claim)
    output.write_text("{not-json", encoding="utf-8")
    observation = adapter.observe_recovery(claim, attempt)
    assert observation.observed_applied is None
    assert observation.observed_identity is None
    assert observation.reread_complete is False
    assert observation.evidence["external_observation"]["operation"] == claim.event_type


def test_projection_fault_tasks_use_distinct_import_provenance(workflow_db):
    factory, _ids, context, reconciliation_task_id = workflow_db
    with session_scope(factory) as session:
        ProjectionService(session).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="Section 4 fault task isolation",
            created_at=NOW,
            external_effects_enabled=True,
        )
    engine = factory.kw["bind"]
    process_task_id = _import_projection_fault_task(
        engine,
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        contract_binding_id=context["binding_id"],
        project_id=context["project_id"],
        section_id=context["section_id"],
        scenario="process-loss",
    )
    disconnect_task_id = _import_projection_fault_task(
        engine,
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        contract_binding_id=context["binding_id"],
        project_id=context["project_id"],
        section_id=context["section_id"],
        scenario="database-disconnect",
    )
    assert len({reconciliation_task_id, process_task_id, disconnect_task_id}) == 3
    with session_scope(factory) as session:
        tasks = [
            session.get(core_models.DishTask, value)
            for value in (process_task_id, disconnect_task_id)
        ]
        assert all(task.creation_route == "import" for task in tasks)
        assert all(task.import_run_id == context["import_run_id"] for task in tasks)
        mapping_count = int(
            session.scalar(
                select(func.count())
                .select_from(projection_models.TaskProjectionMapping)
                .where(
                    projection_models.TaskProjectionMapping.task_id.in_(
                        (process_task_id, disconnect_task_id)
                    ),
                    projection_models.TaskProjectionMapping.state == "active",
                )
            )
            or 0
        )
        prior_event_count = int(
            session.scalar(
                select(func.count())
                .select_from(projection_models.ProjectionOutboxEvent)
                .where(
                    projection_models.ProjectionOutboxEvent.task_id.in_(
                        (process_task_id, disconnect_task_id)
                    )
                )
            )
            or 0
        )
        assert mapping_count == 2
        assert prior_event_count == 0


def test_corpus_manifest_binds_sanitized_corpus(tmp_path):
    corpus, manifest, expected = _corpus_and_manifest(tmp_path)
    loaded = _load_corpus_manifest(manifest, corpus)
    assert loaded["corpus_sha256"] == expected["corpus_sha256"]
    assert loaded["record_count"] == 1
    identity = corpus_identity(corpus, str(expected["corpus_sha256"]))
    parsed_path, parsed_digest = parse_corpus_identity(identity)
    assert parsed_path == corpus.resolve()
    assert parsed_digest == expected["corpus_sha256"]
    items = fetch_sanitized_corpus(identity)
    assert len(items) == 1
    assert items[0].entity_kind == "task"
    assert items[0].payload["title"] == "Sanitized production-shaped task"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"sanitized": False}, "sanitized"),
        ({"resource_scope": "production"}, "resource_scope"),
        ({"production_contact_prohibited": False}, "production_contact_prohibited"),
        ({"contains_production_credentials": True}, "contains_production_credentials"),
        ({"record_count": 2}, "record count"),
        ({"corpus_sha256": "0" * 64}, "SHA-256"),
    ],
)
def test_corpus_manifest_fails_closed(tmp_path, mutation, message):
    corpus, manifest, value = _corpus_and_manifest(tmp_path)
    value.update(mutation)
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProductionShapedError, match=message):
        _load_corpus_manifest(manifest, corpus)


def test_corpus_rejects_production_locator_and_secret_keys(tmp_path):
    corpus, manifest, value = _corpus_and_manifest(tmp_path)
    record = _record(tmp_path)
    record["body"] = "https://api.asana.com/api/1.0/tasks/123"
    corpus.write_text(json.dumps(record) + "\n", encoding="utf-8")
    value["corpus_sha256"] = hashlib.sha256(corpus.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProductionShapedError, match="forbidden production locator"):
        _load_corpus_manifest(manifest, corpus)

    record = _record(tmp_path)
    record["access_token"] = "not-a-real-token"
    corpus.write_text(json.dumps(record) + "\n", encoding="utf-8")
    value["corpus_sha256"] = hashlib.sha256(corpus.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProductionShapedError, match="credential-shaped"):
        _load_corpus_manifest(manifest, corpus)



def test_manifest_identities_reject_production_locators_and_secrets(tmp_path):
    corpus, manifest, value = _corpus_and_manifest(tmp_path)
    value["deployment_identity"] = {"identity": "postgresql://production.example/dish"}
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProductionShapedError, match="forbidden production locator"):
        _load_corpus_manifest(manifest, corpus)

    value["deployment_identity"] = {"identity": "safe", "access_token": "redacted"}
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProductionShapedError, match="credential-shaped"):
        _load_corpus_manifest(manifest, corpus)


def test_corpus_manifest_rejects_unexpected_fields(tmp_path):
    corpus, manifest, value = _corpus_and_manifest(tmp_path)
    value["operator_notes"] = {"access_token": "redacted"}
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProductionShapedError, match="fields mismatch"):
        _load_corpus_manifest(manifest, corpus)


def test_corpus_rejects_nested_secret_keys(tmp_path):
    corpus, manifest, value = _corpus_and_manifest(tmp_path)
    record = _record(tmp_path)
    record["metadata"] = {"private_token": "redacted"}
    corpus.write_text(json.dumps(record) + "\n", encoding="utf-8")
    value["corpus_sha256"] = hashlib.sha256(corpus.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProductionShapedError, match="credential-shaped"):
        _load_corpus_manifest(manifest, corpus)

def test_safe_runner_does_not_inherit_ambient_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "must-not-cross-boundary")
    output = tmp_path / "env.json"
    runner = SafeRunner(tmp_path / "logs")
    runner.run(
        [
            sys.executable,
            "-c",
            "import json,os,sys; json.dump(dict(os.environ), open(sys.argv[1], 'w'))",
            output,
        ],
        timeout_seconds=10,
    )
    child = json.loads(output.read_text(encoding="utf-8"))
    assert "ASANA_ACCESS_TOKEN" not in child
    assert child["PYTHONNOUSERSITE"] == "1"
    with pytest.raises(ProductionShapedError, match="may not inherit"):
        runner.run([sys.executable, "-c", "pass"], timeout_seconds=10, inherit_env=True)


def test_base_runner_can_execute_with_an_exact_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_TEST_PARENT_SECRET", "parent")
    runner = Runner(tmp_path / "logs")
    completed = runner.run(
        [sys.executable, "-c", "import os; print(os.environ.get('DISH_TEST_PARENT_SECRET')); print(os.environ['ONLY_THIS'])"],
        timeout_seconds=10,
        env={"ONLY_THIS": "present"},
        inherit_env=False,
    )
    assert completed.stdout.splitlines() == ["None", "present"]


def test_safe_environment_rejects_credential_shaped_additions():
    with pytest.raises(ProductionShapedError, match="unsafe child environment"):
        _safe_child_env({"MY_SECRET": "value"})


def test_phase_recorder_distinguishes_passed_blocked_and_implemented():
    recorder = PhaseRecorder()
    recorder.run("postgresql_migration", lambda: {"migration_status": "passed"})
    details = recorder.blocked(
        "corpus_import",
        reason="native runtime unavailable",
        details={"execution_status": "implemented_but_blocked"},
    )
    assert details["implementation_status"] == "implemented"
    assert recorder.items[0].status == "passed"
    assert recorder.items[1].status == "blocked"
    assert recorder.items[1].availability_status == "blocked_runtime_infrastructure"
    assert recorder.items[1].first_attempt_status == "blocked"



def test_exact_checkout_identity_requires_expected_clean_commit(tmp_path):
    repository = tmp_path / "honest"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", repository], check=True)
    subprocess.run(
        ["git", "-C", repository, "config", "user.email", "section4@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repository, "config", "user.name", "Section 4 test"],
        check=True,
    )
    (repository / "DISH_VERSION").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", repository, "add", "DISH_VERSION"], check=True)
    subprocess.run(["git", "-C", repository, "commit", "-q", "-m", "fixture"], check=True)
    head = subprocess.run(
        ["git", "-C", repository, "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    identity = _checkout_identity(
        SafeRunner(tmp_path / "logs"),
        repository,
        expected_commit=head,
        label="Honest checkout",
    )
    assert identity["commit"] == head
    assert len(identity["tree"]) == 40
    (repository / "DISH_VERSION").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ProductionShapedError, match="worktree must be clean"):
        _checkout_identity(
            SafeRunner(tmp_path / "dirty-logs"),
            repository,
            expected_commit=head,
            label="Honest checkout",
        )


def test_describe_input_identities_binds_source_and_deployment(monkeypatch):
    monkeypatch.setattr(
        "dish_pg.production_shaped_rehearsal._source_identity",
        lambda *args, **kwargs: {
            "source_manifest": {"manifest_sha256": "a" * 64},
            "current_commit": "b" * 40,
        },
    )
    monkeypatch.setattr(
        "dish_pg.production_shaped_rehearsal._deployment_configuration_identity",
        lambda: {"identity": "c" * 64},
    )
    value = _describe_input_identities(
        argparse.Namespace(
            dish_commit=None,
            base_commit=None,
            source_identity_kind="git_commit",
        )
    )
    assert value["schema"] == "dish-section4-input-identities-v1"
    assert value["source_manifest_identity"] == "a" * 64
    assert value["deployment_identity"] == "c" * 64

def test_source_manifest_covers_section4_and_section2_reuse():
    manifest = _source_manifest()
    paths = {item["path"] for item in manifest["files"]}
    assert set(SOURCE_IDENTITY_PATHS) == paths
    assert "dish_pg/production_shaped_rehearsal.py" in paths
    assert "dish_pg/production_shaped_support.py" in paths
    assert "dish_pg/production_shaped_runtime.py" in paths
    assert "dish_pg/recovery_rehearsal.py" in paths
    assert "dish_pg/process_failure_rehearsal.py" in paths
    assert "scripts/dish-pg-production-shaped-rehearsal" in paths
    assert manifest["alembic_head"]
    assert len(manifest["manifest_sha256"]) == 64



def test_manifest_bindings_match_current_source_and_deployment():
    source = {"source_manifest": {"manifest_sha256": "a" * 64}}
    deployment = {"identity": "b" * 64}
    manifest = {
        "source_manifest": {"identity": "a" * 64},
        "deployment_identity": {"identity": "b" * 64},
    }
    _validate_manifest_bindings(
        manifest, source_identity=source, deployment_identity=deployment
    )
    manifest["source_manifest"]["identity"] = "c" * 64
    with pytest.raises(ProductionShapedError, match="source_manifest identity"):
        _validate_manifest_bindings(
            manifest, source_identity=source, deployment_identity=deployment
        )


def test_deployment_identity_is_stable_and_bound_to_files():
    first = _deployment_configuration_identity()
    second = _deployment_configuration_identity()
    assert first == second
    assert first["schema"] == "dish-section4-deployment-config-v1"
    assert {item["path"] for item in first["files"]} == {
        "deploy/postgresql/compose.yaml",
        "alembic.ini",
        "requirements.txt",
    }

def test_report_hash_is_stable_and_excludes_its_own_field():
    first = _report_hash({"schema": REPORT_SCHEMA, "status": "blocked"})
    second = _report_hash(dict(first))
    assert first["report_sha256"] == second["report_sha256"]


def test_isolation_inputs_reject_repository_and_production_paths(tmp_path):
    corpus, manifest, _ = _corpus_and_manifest(tmp_path)
    honest = tmp_path / "honest"
    honest.mkdir()
    base = dict(
        report=tmp_path / "report.json",
        evidence_dir=tmp_path / "evidence",
        work_root=tmp_path / "work",
        corpus=corpus,
        corpus_manifest=manifest,
        honest_repo=honest,
    )
    _validate_isolation_inputs(argparse.Namespace(**base))

    base["work_root"] = Path(__file__).resolve().parents[2] / "unsafe-work"
    with pytest.raises(ProductionShapedError, match="outside the repository"):
        _validate_isolation_inputs(argparse.Namespace(**base))

    base["work_root"] = tmp_path / "work"
    base["honest_repo"] = Path("/home/marco/honest-pantry")
    with pytest.raises(ProductionShapedError, match="forbidden production path"):
        _validate_isolation_inputs(argparse.Namespace(**base))


def test_missing_native_postgresql_produces_bound_blocked_report(tmp_path, monkeypatch):
    corpus, manifest, _ = _corpus_and_manifest(tmp_path)
    monkeypatch.setattr(
        "dish_pg.production_shaped_rehearsal._source_identity",
        lambda *args, **kwargs: {
            "kind": "git_commit",
            "current_commit": "a" * 40,
            "current_tree": "b" * 40,
            "parent_commit": "c" * 40,
            "source_manifest": {"manifest_sha256": "source-test"},
        },
    )
    monkeypatch.setattr(
        "dish_pg.production_shaped_rehearsal._checkout_identity",
        lambda *args, **kwargs: {
            "path": str(tmp_path / "honest"),
            "commit": "e" * 40,
            "tree": "f" * 40,
            "worktree_clean": True,
        },
    )
    monkeypatch.setattr(
        "dish_pg.production_shaped_rehearsal._deployment_configuration_identity",
        lambda: {
            "schema": "dish-section4-deployment-config-v1",
            "files": [],
            "identity": "deployment-test",
        },
    )
    monkeypatch.setattr(
        "dish_pg.production_shaped_rehearsal.discover_pg_bin",
        lambda value: (_ for _ in ()).throw(
            RehearsalBlocked("native PostgreSQL unavailable", missing_commands=("initdb", "postgres"))
        ),
    )
    honest = tmp_path / "honest"
    honest.mkdir()
    args = argparse.Namespace(
        report=tmp_path / "report.json",
        evidence_dir=tmp_path / "evidence",
        work_root=tmp_path / "work",
        corpus=corpus,
        corpus_manifest=manifest,
        honest_repo=honest,
        honest_commit="e" * 40,
        source_generation="test",
        repository_input_identity="archive-sha256:" + "1" * 64,
        project_id="1ae6e7ba-31e3-5dc5-9565-4ea37b49ac97",
        project_gid="1216693403164366",
        project_name="test",
        section_id="8b5bfb31-b986-5116-a207-569a5ba95907",
        section_gid="1216891250619908",
        section_name="test",
        pg_bin=None,
        port_base=56640,
        dish_commit=None,
        base_commit=None,
        source_identity_kind="git_commit",
        keep_resources=True,
    )
    report = run(args)
    assert report["schema"] == REPORT_SCHEMA
    assert report["status"] == "blocked"
    assert report["source_identity"]["current_commit"] == "a" * 40
    assert report["sanitized_corpus"]["corpus_sha256"]
    assert [item["first_attempt_status"] for item in report["command_inventory"]] == ["not_attempted"] * 3
    assert len(report["phases"]) == len(PHASES)
    assert all(item["implementation_status"] == "implemented" for item in report["phases"])
    assert all(item["status"] == "blocked" for item in report["phases"])
    assert all(item["details"]["execution_status"] == "implemented_but_blocked" for item in report["phases"])
    assert report["implemented_and_passed_phases"] == []
    assert report["implemented_but_blocked_phases"] == list(PHASES)
    assert report["failed_phases"] == []
    assert report["not_implemented_phases"] == []
    assert "missing_native_commands:initdb,postgres" in report["blocked_scenarios"]
    assert report["local_measurement_limitations"]["production_rpo_claimed"] is False
    assert report["local_measurement_limitations"]["production_rto_claimed"] is False
    assert report["report_sha256"]
