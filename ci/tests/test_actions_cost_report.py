from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "actions_cost_report.py"
CONFIG_PATH = ROOT / "ci" / "actions-billing.json"
HISTORICAL_FIXTURE = ROOT / "ci" / "fixtures" / "actions-run-31697885898-jobs.json"


def _module():
    spec = importlib.util.spec_from_file_location("actions_cost_report", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _job(*, seconds: int, conclusion: str = "success", runner_id: int | None = 1):
    return {
        "id": 1,
        "workflow_name": "CI",
        "name": "example",
        "started_at": "2026-08-13T00:00:00Z",
        "completed_at": f"2026-08-13T00:{seconds // 60:02d}:{seconds % 60:02d}Z",
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
    fixture = json.loads(HISTORICAL_FIXTURE.read_text(encoding="utf-8"))
    config = module.load_billing_config(CONFIG_PATH)

    report = module.build_report(fixture["jobs"], config=config)

    assert fixture["source"]["run_id"] == 31697885898
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
