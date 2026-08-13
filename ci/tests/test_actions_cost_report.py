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
HISTORICAL_FIXTURE = ROOT / "ci" / "fixtures" / "actions-run-31697885898-jobs.json"
SINCE = "2026-08-01T00:00:00Z"
UNTIL = "2026-08-31T23:59:59Z"


def _module():
    spec = importlib.util.spec_from_file_location("actions_cost_report", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _job(*, seconds: int, job_id: int = 1, conclusion: str = "success", runner_id=1):
    started = datetime(2026, 8, 13, tzinfo=timezone.utc) + timedelta(seconds=job_id)
    return {
        "id": job_id,
        "workflow_name": "CI",
        "name": "example",
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "completed_at": (started + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"),
        "conclusion": conclusion,
        "labels": ["ubuntu-24.04"],
        "runner_id": runner_id,
    }


def _report(module, jobs, config):
    return module.build_report(jobs, config=config, since=SINCE, until=UNTIL)


def test_per_job_billing_rounds_each_started_job_up_independently() -> None:
    module = _module()
    assert module.billed_minutes(_job(seconds=1)) == 1
    assert module.billed_minutes(_job(seconds=60)) == 1
    assert module.billed_minutes(_job(seconds=61)) == 2
    assert module.billed_minutes(_job(seconds=0)) == 1
    assert module.billed_minutes(_job(seconds=0, conclusion="skipped", runner_id=None)) == 0
    assert module.billed_minutes(_job(seconds=2, conclusion="failure", runner_id=0)) == 0


def test_historical_23_minutes_are_gross_cost_but_zero_overage() -> None:
    module = _module()
    fixture = json.loads(HISTORICAL_FIXTURE.read_text(encoding="utf-8"))
    report = _report(module, fixture["jobs"], module.load_billing_config(CONFIG_PATH))

    assert fixture["source"]["run_id"] == 31697885898
    assert report["totals"] == {
        "jobs": 7,
        "runtime_seconds": 1074,
        "billed_minutes": 23,
        "cancelled_billed_minutes": 0,
        "included_minutes_consumed": 23,
        "remaining_included_minutes": 1977,
        "overage_billed_minutes": 0,
        "gross_equivalent_cost_usd": 0.138,
        "approximate_overage_cost_usd": 0.0,
    }
    assert report["by_workflow"]["CI"]["gross_equivalent_cost_usd"] == 0.138
    ci_jobs = report["by_workflow"]["CI"]["by_job"]
    assert ci_jobs["Broad Python tests"]["billed_minutes"] == 11
    assert ci_jobs["Native PostgreSQL"]["billed_minutes"] == 5
    assert ci_jobs["Browser acceptance"]["billed_minutes"] == 3
    assert ci_jobs["Frontend and tooling"]["billed_minutes"] == 1


def test_only_minutes_above_allowance_are_overage_cost() -> None:
    module = _module()
    config = dict(module.load_billing_config(CONFIG_PATH))
    config["included_minutes_per_month"] = 2
    report = _report(module, [_job(seconds=60, job_id=1), _job(seconds=61, job_id=2)], config)

    assert report["totals"]["billed_minutes"] == 3
    assert report["totals"]["included_minutes_consumed"] == 2
    assert report["totals"]["overage_billed_minutes"] == 1
    assert report["totals"]["gross_equivalent_cost_usd"] == 0.018
    assert report["totals"]["approximate_overage_cost_usd"] == 0.006


def test_allowance_accounting_requires_one_complete_utc_month() -> None:
    module = _module()
    config = module.load_billing_config(CONFIG_PATH)
    with pytest.raises(module.CostReportError, match="first instant"):
        module.build_report([_job(seconds=1)], config=config, since="2026-08-02T00:00:00Z", until=UNTIL)
    with pytest.raises(module.CostReportError, match="final second"):
        module.build_report([_job(seconds=1)], config=config, since=SINCE, until="2026-09-30T23:59:59Z")


def test_billable_job_outside_declared_month_fails_closed() -> None:
    module = _module()
    config = module.load_billing_config(CONFIG_PATH)
    job = _job(seconds=30)
    job["started_at"] = "2026-09-01T00:00:00Z"
    job["completed_at"] = "2026-09-01T00:00:30Z"
    with pytest.raises(module.CostReportError, match="outside billing month 2026-08"):
        _report(module, [job], config)


def test_cancelled_minutes_deduplicate_and_unknown_runner_fails_closed() -> None:
    module = _module()
    config = module.load_billing_config(CONFIG_PATH)
    job = _job(seconds=61, conclusion="cancelled")
    report = _report(module, [job, dict(job)], config)
    assert report["totals"]["billed_minutes"] == 2
    assert report["totals"]["cancelled_billed_minutes"] == 2
    assert report["totals"]["gross_equivalent_cost_usd"] == 0.012
    assert report["totals"]["approximate_overage_cost_usd"] == 0.0

    unknown = _job(seconds=30, job_id=2)
    unknown["labels"] = ["windows-2025"]
    with pytest.raises(module.CostReportError, match="exactly one configured billing label"):
        _report(module, [unknown], config)
