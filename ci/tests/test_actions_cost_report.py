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


def _module():
    spec = importlib.util.spec_from_file_location("actions_cost_report", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _job(*, seconds: int, name: str = "example", job_id: int = 1, conclusion: str = "success", runner_id: int | None = 1):
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


def test_per_job_billing_rounds_each_started_job_independently() -> None:
    module = _module()
    assert module.billed_minutes(_job(seconds=1)) == 1
    assert module.billed_minutes(_job(seconds=59)) == 1
    assert module.billed_minutes(_job(seconds=60)) == 1
    assert module.billed_minutes(_job(seconds=61)) == 2
    assert module.billed_minutes(_job(seconds=0)) == 1
    assert module.billed_minutes(_job(seconds=0, conclusion="skipped", runner_id=None)) == 0


def test_historical_ci_example_keeps_23_billed_minutes_but_zero_overage_with_allowance() -> None:
    module = _module()
    durations = [4, 5, 164, 246, 605, 39, 11]
    names = [
        "Begin exact-head ordinary CI gate",
        "Dependency bundle metadata",
        "Browser acceptance",
        "Native PostgreSQL",
        "Broad Python tests",
        "Frontend and tooling",
        "Exact-head ordinary CI gate",
    ]
    jobs = [_job(seconds=s, name=n, job_id=i) for i, (s, n) in enumerate(zip(durations, names), start=1)]
    report = module.build_report(jobs, config=module.load_billing_config(CONFIG_PATH))
    assert report["totals"]["runtime_seconds"] == 1074
    assert report["totals"]["billed_minutes"] == 23
    assert report["totals"]["gross_equivalent_cost_usd"] == 0.138
    assert report["totals"]["overage_billed_minutes"] == 0
    assert report["totals"]["approximate_overage_cost_usd"] == 0.0


def test_allowance_overage_charges_only_incremental_excess_minutes() -> None:
    module = _module()
    config = module.load_billing_config(CONFIG_PATH)
    jobs = [_job(seconds=61, job_id=i) for i in range(1, 13)]  # 24 billed minutes
    report = module.build_report(
        jobs,
        config=config,
        monthly_billed_minutes_before_period=1990,
    )
    assert report["totals"]["billed_minutes"] == 24
    assert report["totals"]["overage_billed_minutes"] == 14
    assert report["totals"]["approximate_overage_cost_usd"] == 0.084


def test_report_counts_cancelled_started_minutes_and_deduplicates_job_ids() -> None:
    module = _module()
    config = module.load_billing_config(CONFIG_PATH)
    job = _job(seconds=61, conclusion="cancelled")
    report = module.build_report([job, dict(job)], config=config)
    assert report["totals"]["jobs"] == 1
    assert report["totals"]["billed_minutes"] == 2
    assert report["totals"]["cancelled_billed_minutes"] == 2
    assert report["totals"]["gross_equivalent_cost_usd"] == 0.012


def test_unknown_runner_rate_and_ambiguous_period_fail_closed() -> None:
    module = _module()
    config = module.load_billing_config(CONFIG_PATH)
    job = _job(seconds=30)
    job["labels"] = ["windows-2025"]
    with pytest.raises(module.CostReportError, match="exactly one configured billing label"):
        module.build_report([job], config=config)

    with pytest.raises(module.CostReportError, match="within one UTC calendar month"):
        module.build_report(
            [],
            config=config,
            since="2026-08-01T00:00:00Z",
            until="2026-09-01T00:00:00Z",
        )

    with pytest.raises(module.CostReportError, match="monthly_billed_minutes_before_period"):
        module.build_report(
            [],
            config=config,
            since="2026-08-13T00:00:00Z",
            until="2026-08-14T00:00:00Z",
        )
