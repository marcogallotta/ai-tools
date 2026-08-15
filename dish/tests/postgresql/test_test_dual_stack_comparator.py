from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dish_pg import test_comparator as comparator


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "deploy/comparator/test-qualification-plan.json"


def _envs() -> tuple[dict[str, str], dict[str, str]]:
    authority = {
        "DISH_AUTHORITY_BACKEND": "postgresql",
        "DISH_PROFILE": "test",
        "DISH_SERVICE_BIND": "127.0.0.1",
        "DISH_ACTION_BIND": "127.0.0.1",
        "DISH_SERVICE_PORT": "8765",
        "DISH_ACTION_PORT": "8766",
        "DISH_SERVICE_ACTION_TOKEN": "authority-token",
        "DISH_PG_DATABASE_URL": "postgresql+psycopg://dish:secret@127.0.0.1:55432/dish_stage_a_test",
        "DISH_PG_EXPECTED_DATABASE_NAME": "dish_stage_a_test",
        "DISH_PG_AUTHORITY_STATE_DIR": "/home/marco/.local/state/dish/test/pg-authority",
        "DISH_DARK_LAUNCH_MODE": "off",
    }
    oracle = {
        "DISH_AUTHORITY_BACKEND": "legacy",
        "DISH_TEST_COMPARATOR_DISPOSABLE": "1",
        "DISH_SERVICE_BIND": "127.0.0.1",
        "DISH_ACTION_BIND": "127.0.0.1",
        "DISH_SERVICE_PORT": "8795",
        "DISH_ACTION_PORT": "8796",
        "DISH_COOKING_PROJECT_GID": comparator.DISPOSABLE_ORACLE_PROJECT_GID,
        "DISH_DB_PATH": "/home/marco/.local/state/dish/test-legacy/shared.sqlite3",
        "DISH_SERVICE_BACKUP_DIR": "/home/marco/.local/state/dish/test-legacy/backups",
        "DISH_SERVICE_ACTION_TOKEN": "oracle-token",
        "ASANA_ENV": "/home/marco/.config/asana-cli/.env",
        "DISH_DARK_LAUNCH_MODE": "off",
    }
    return authority, oracle


def _targets() -> tuple[comparator.TargetConfig, comparator.TargetConfig]:
    authority, oracle = _envs()
    return (
        comparator.TargetConfig("authority", "http://authority/test", "http://authority/health", "authority-token", Path("/authority.env"), authority),
        comparator.TargetConfig("oracle", "http://oracle/test-legacy", "http://oracle/health", "oracle-token", Path("/oracle.env"), oracle),
    )


def _preflight() -> dict[str, object]:
    return {"action_contract": {"match": True}, "routing": {}, "authority_health": {}, "oracle_health": {}}


def test_plan_is_curated_and_mutation_is_explicit() -> None:
    plan = comparator.load_plan(PLAN)
    assert [item["id"] for item in plan["scenarios"]] == ["sections", "create-bare-dish", "read-created-dish"]
    assert [item["id"] for item in plan["scenarios"] if item["mutating"]] == ["create-bare-dish"]


def test_authority_rejects_asana_and_oracle_requires_disposable_isolation() -> None:
    authority, oracle = _envs()
    authority["ASANA_ENV"] = "/tmp/asana"
    with pytest.raises(comparator.ComparatorError, match="no populated Asana"):
        comparator.validate_target_environments(authority, oracle)
    authority.pop("ASANA_ENV")
    oracle["DISH_TEST_COMPARATOR_DISPOSABLE"] = "0"
    with pytest.raises(comparator.ComparatorError, match="DISH_TEST_COMPARATOR_DISPOSABLE"):
        comparator.validate_target_environments(authority, oracle)
    oracle["DISH_TEST_COMPARATOR_DISPOSABLE"] = "1"
    oracle["DISH_DB_PATH"] = "/home/marco/.local/state/dish/test/shared.sqlite3"
    with pytest.raises(comparator.ComparatorError, match="test-legacy"):
        comparator.validate_target_environments(authority, oracle)


def test_oracle_rejects_dotdot_escape_from_disposable_state_root() -> None:
    authority, oracle = _envs()
    oracle["DISH_DB_PATH"] = "/home/marco/.local/state/dish/test-legacy/../test/shared.sqlite3"
    with pytest.raises(comparator.ComparatorError, match="after canonicalization"):
        comparator.validate_target_environments(authority, oracle)


def test_route_preflight_rejects_wrong_self_consistent_oracle_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, oracle = _targets()
    wrong_project_gid = "1210000000000000"
    oracle.env["DISH_COOKING_PROJECT_GID"] = wrong_project_gid
    responses = {
        authority.health_url: (200, {"ok": True, "backend": "postgresql", "profile": "test", "isolation": {"asana_environment_keys": []}}),
        oracle.health_url: (200, {"ok": True, "asana": {"ok": True}}),
        authority.action_base + "/openapi/action.json": (200, {"paths": {"/v1/action/sections": {}}}),
        oracle.action_base + "/openapi/action.json": (200, {"paths": {"/v1/action/sections": {}}}),
    }
    monkeypatch.setattr(comparator, "_request_json", lambda url, **_kwargs: responses[url])
    monkeypatch.setattr(
        comparator,
        "_command_request",
        lambda target, **_kwargs: {
            "data": {"project_gid": None if target.name == "authority" else wrong_project_gid}
        },
    )
    with pytest.raises(comparator.ComparatorError, match="repository-designated disposable TEST project"):
        comparator._route_preflight(authority, oracle, run_id="run")


def test_normalization_removes_declared_identity_timestamp_and_uuid_noise() -> None:
    value = {
        "task_gid": "abc",
        "when": "2026-08-15T08:00:00Z",
        "request": "0f8fad5b-d9cb-469f-a165-70867728950e",
        "created": "legacy-123",
    }
    assert comparator.normalize_value(value, drop_keys=frozenset({"task_gid"}), identity_aliases={"legacy-123": "<identity:created>"}) == {
        "created": "<identity:created>", "request": "<uuid>", "when": "<timestamp>"
    }


def test_read_only_run_is_diagnostic_and_persists_secret_free_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, oracle = _targets()
    plan = comparator.load_plan(PLAN)
    monkeypatch.setattr(comparator, "_route_preflight", lambda *_args, **_kwargs: _preflight())
    monkeypatch.setattr(comparator, "_command_request", lambda *_args, **_kwargs: {"ok": True, "command": "sections", "data": {"sections": []}})
    outcome = comparator.run_comparison(plan=plan, authority=authority, oracle=oracle, evidence_dir=tmp_path, run_id=uuid.UUID(int=1), now=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert outcome.report["mismatch_count"] == 0
    assert outcome.report["skipped_count"] == 2
    assert outcome.report["full_qualification"] is False
    assert outcome.report["qualification_passed"] is False
    evidence = outcome.evidence_path.read_text()
    assert "authority-token" not in evidence and "oracle-token" not in evidence
    assert json.loads((tmp_path / "latest.json").read_text())["comparison_run_id"] == str(uuid.UUID(int=1))


def test_full_run_matches_equivalent_target_specific_identities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, oracle = _targets()
    plan = comparator.load_plan(PLAN)
    monkeypatch.setattr(comparator, "_route_preflight", lambda *_args, **_kwargs: _preflight())

    def request(target: comparator.TargetConfig, *, command: str, arguments: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if command == "sections":
            return {"ok": True, "command": "sections", "data": {"sections": []}}
        if command == "create":
            if target.name == "authority":
                return {"ok": True, "command": "create", "code": "OK", "retryable": False, "data": {"dish_id": "pg-dish"}}
            return {"ok": True, "command": "create", "code": "OK", "retryable": False, "data": {"task_gid": "asana-task"}}
        assert command == "read"
        if target.name == "authority":
            assert arguments["dish_id"] == "pg-dish"
            return {"ok": True, "command": "read", "data": {"title": "same", "body": "body", "completed": False}}
        assert arguments["task_gid"] == "asana-task"
        return {"ok": True, "command": "read", "data": {"task": {"title": "same", "notes": "body", "completed": False}}}

    monkeypatch.setattr(comparator, "_command_request", request)
    outcome = comparator.run_comparison(plan=plan, authority=authority, oracle=oracle, evidence_dir=tmp_path, allow_mutating_scenarios=True, run_id=uuid.UUID(int=2))
    assert outcome.report["mismatch_count"] == 0
    assert outcome.report["skipped_count"] == 0
    assert outcome.report["full_qualification"] is True
    assert outcome.report["qualification_passed"] is True


def test_material_mismatch_is_durable_and_blocks_qualification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, oracle = _targets()
    plan = {"format": comparator.PLAN_FORMAT, "name": "mismatch", "scenarios": [{"id": "sections", "command": "sections", "mutating": False, "arguments": {}}]}
    monkeypatch.setattr(comparator, "_route_preflight", lambda *_args, **_kwargs: _preflight())
    monkeypatch.setattr(comparator, "_command_request", lambda target, **_kwargs: {"ok": True, "data": {"value": target.name}})
    outcome = comparator.run_comparison(plan=plan, authority=authority, oracle=oracle, evidence_dir=tmp_path, allow_mutating_scenarios=True)
    assert outcome.report["mismatch_count"] == 1
    assert outcome.report["qualification_passed"] is False
    assert json.loads(outcome.evidence_path.read_text())["status"] == "mismatch"


def test_route_preflight_rejects_legacy_identity_on_default_test_route(monkeypatch: pytest.MonkeyPatch) -> None:
    authority, oracle = _targets()
    responses = {
        authority.health_url: (200, {"ok": True, "backend": "postgresql", "profile": "test", "isolation": {"asana_environment_keys": []}}),
        oracle.health_url: (200, {"ok": True, "asana": {"ok": True}}),
        authority.action_base + "/openapi/action.json": (200, {"paths": {"/v1/action/sections": {}}}),
        oracle.action_base + "/openapi/action.json": (200, {"paths": {"/v1/action/sections": {}}}),
    }
    monkeypatch.setattr(comparator, "_request_json", lambda url, **_kwargs: responses[url])
    monkeypatch.setattr(comparator, "_command_request", lambda target, **_kwargs: {"data": {"project_gid": "legacy" if target.name == "authority" else comparator.DISPOSABLE_ORACLE_PROJECT_GID}})
    with pytest.raises(comparator.ComparatorError, match="default TEST Action route returned legacy project identity"):
        comparator._route_preflight(authority, oracle, run_id="run")
