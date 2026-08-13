from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "actions_cost_report.py"
CONFIG_PATH = ROOT / "ci" / "actions-billing.json"
CERTIFICATION_MODULE_PATH = ROOT / "scripts" / "integration_certification.py"
ACTION = ROOT / ".github" / "actions" / "run-certification" / "action.yml"


def _module():
    spec = importlib.util.spec_from_file_location("actions_cost_report", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _job(
    *,
    seconds: int,
    name: str = "example",
    job_id: int = 1,
    conclusion: str = "success",
    runner_id: int | None = 1,
):
    start = datetime(2026, 8, 13, tzinfo=timezone.utc)
    end = start + timedelta(seconds=seconds)
    return {
        "id": job_id,
        "workflow_name": "CI",
        "name": name,
        "started_at": start.isoformat().replace("+00:00", "Z"),
        "completed_at": end.isoformat().replace("+00:00", "Z"),
        "conclusion": conclusion,
        "labels": ["ubuntu-24.04"],
        "runner_id": runner_id,
    }


def test_per_job_billing_rounds_each_started_job_up_independently() -> None:
    module = _module()

    assert module.billed_minutes(_job(seconds=1)) == 1
    assert module.billed_minutes(_job(seconds=59)) == 1
    assert module.billed_minutes(_job(seconds=60)) == 1
    assert module.billed_minutes(_job(seconds=61)) == 2
    assert module.billed_minutes(_job(seconds=0)) == 1
    assert module.billed_minutes(_job(seconds=0, conclusion="skipped", runner_id=None)) == 0
    assert module.billed_minutes(_job(seconds=2, conclusion="failure", runner_id=0)) == 0


def test_historical_ci_run_reproduces_observed_23_billed_minutes() -> None:
    module = _module()
    # GitHub Actions run 31697885898. Durations are captured from its jobs API.
    historical_jobs = [
        ("Begin exact-head ordinary CI gate", 4),
        ("Dependency bundle metadata", 5),
        ("Browser acceptance", 164),
        ("Native PostgreSQL", 246),
        ("Broad Python tests", 605),
        ("Frontend and tooling", 39),
        ("Exact-head ordinary CI gate", 11),
    ]
    jobs = [
        _job(name=name, seconds=seconds, job_id=index)
        for index, (name, seconds) in enumerate(historical_jobs, start=1)
    ]
    config = module.load_billing_config(CONFIG_PATH)

    report = module.build_report(jobs, config=config)

    assert report["totals"] == {
        "jobs": 7,
        "runtime_seconds": 1074,
        "billed_minutes": 23,
        "cancelled_billed_minutes": 0,
        "approximate_cost_usd": 0.138,
    }
    ci_jobs = report["by_workflow"]["CI"]["by_job"]
    assert ci_jobs["Broad Python tests"]["billed_minutes"] == 11
    assert ci_jobs["Native PostgreSQL"]["billed_minutes"] == 5
    assert ci_jobs["Browser acceptance"]["billed_minutes"] == 3
    assert ci_jobs["Frontend and tooling"]["billed_minutes"] == 1


def test_report_counts_cancelled_started_minutes_and_deduplicates_job_ids() -> None:
    module = _module()
    config = module.load_billing_config(CONFIG_PATH)
    job = _job(seconds=61, conclusion="cancelled")

    report = module.build_report([job, dict(job)], config=config)

    assert report["totals"]["jobs"] == 1
    assert report["totals"]["billed_minutes"] == 2
    assert report["totals"]["cancelled_billed_minutes"] == 2
    assert report["totals"]["approximate_cost_usd"] == 0.012


def test_unknown_runner_rate_fails_closed() -> None:
    module = _module()
    config = module.load_billing_config(CONFIG_PATH)
    job = _job(seconds=30)
    job["labels"] = ["windows-2025"]

    with pytest.raises(module.CostReportError, match="exactly one configured billing label"):
        module.build_report([job], config=config)


def _certification_module():
    spec = importlib.util.spec_from_file_location("integration_certification", CERTIFICATION_MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_spec(tmp_path: Path, required_groups: dict[str, list[dict[str, object]]]) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(
        json.dumps(
            {
                "schema": "dish-certification-execution-spec-v1",
                "candidate_sha": "a" * 40,
                "plan_digest": "b" * 64,
                "required_groups": required_groups,
            }
        ),
        encoding="utf-8",
    )
    return path


def _command(name: str) -> list[dict[str, object]]:
    return [{"name": name, "argv": ["true"]}]


def test_runtime_requirements_are_derived_only_from_selected_groups(tmp_path: Path) -> None:
    module = _certification_module()

    frontend = module.load_execution_spec(
        _write_spec(tmp_path, {"frontend-static": _command("frontend")})
    )
    assert module.setup_requirements(frontend) == {
        "python": False,
        "node": True,
        "postgresql": False,
        "chromium": False,
    }

    browser = module.load_execution_spec(
        _write_spec(tmp_path, {"browser-acceptance": _command("browser")})
    )
    assert module.setup_requirements(browser) == {
        "python": True,
        "node": True,
        "postgresql": False,
        "chromium": True,
    }

    postgres = module.load_execution_spec(
        _write_spec(tmp_path, {"native-postgresql": _command("postgres")})
    )
    assert module.setup_requirements(postgres) == {
        "python": True,
        "node": False,
        "postgresql": True,
        "chromium": False,
    }


def test_execution_is_deterministic_and_fail_fast_with_complete_group_evidence(tmp_path: Path) -> None:
    module = _certification_module()
    spec = module.load_execution_spec(
        _write_spec(
            tmp_path,
            {
                "browser-acceptance": _command("browser"),
                "native-postgresql": _command("postgres"),
                "python-control-plane": _command("python"),
            },
        )
    )
    calls: list[str] = []

    def runner(command, _root, _log):
        calls.append(command.name)
        return 7 if command.name == "postgres" else 0

    ticks = iter([0.0, 1.0, 3.5, 4.0, 9.0, 9.0])
    evidence_path = tmp_path / "evidence.json"
    payload = module.execute_certification(
        spec,
        run_id="12345",
        run_attempt=2,
        repo_root=tmp_path,
        evidence_path=evidence_path,
        command_runner=runner,
        clock=lambda: next(ticks),
    )

    assert calls == ["python", "postgres"]
    assert payload["execution_order"] == list(module.GROUP_ORDER)
    assert payload["required_groups"] == [
        "python-control-plane",
        "native-postgresql",
        "browser-acceptance",
    ]
    assert payload["group_results"] == {
        "python-control-plane": {"result": "passed", "elapsed_seconds": 2.5},
        "frontend-static": {"result": "not_selected", "elapsed_seconds": 0.0},
        "native-postgresql": {"result": "failed", "elapsed_seconds": 5.0},
        "browser-acceptance": {
            "result": "not_run_due_to_prior_failure",
            "elapsed_seconds": 0.0,
        },
    }
    assert payload["candidate_sha"] == "a" * 40
    assert payload["plan_digest"] == "b" * 64
    assert payload["run_id"] == "12345"
    assert payload["run_attempt"] == 2
    assert payload["elapsed_seconds"] == 9.0
    assert payload["outcome"] == "failed"
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == payload


def test_execution_spec_rejects_shell_strings_and_unknown_groups(tmp_path: Path) -> None:
    module = _certification_module()
    path = _write_spec(
        tmp_path,
        {
            "python-control-plane": [{"name": "bad", "argv": "pytest -q"}],
            "mystery-group": _command("mystery"),
        },
    )
    with pytest.raises(module.CertificationError, match="unknown execution groups"):
        module.load_execution_spec(path)

    path = _write_spec(
        tmp_path,
        {"python-control-plane": [{"name": "bad", "argv": "pytest -q"}]},
    )
    with pytest.raises(module.CertificationError, match="argv must be"):
        module.load_execution_spec(path)


def test_composite_action_keeps_all_heavy_setup_conditional() -> None:
    action = ACTION.read_text(encoding="utf-8")

    assert "uses: actions/setup-python@v6" in action
    assert "uses: actions/setup-node@v6" in action
    assert "uses: ./.github/actions/setup-python-bundle" in action
    assert "postgres:17.10" in action
    assert "playwright install --with-deps chromium" in action

    python_if = "if: steps.runtime.outputs.python == 'true'"
    node_if = "if: steps.runtime.outputs.node == 'true'"
    pg_if = "if: steps.runtime.outputs.postgresql == 'true'"
    chromium_if = "if: steps.runtime.outputs.chromium == 'true'"
    assert action.count(python_if) >= 3
    assert action.count(node_if) == 1
    assert action.count(pg_if) == 1
    assert action.count(chromium_if) == 1
    assert "runs-on:" not in action
    assert "jobs:" not in action
