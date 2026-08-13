import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module():
    path = ROOT / "scripts" / "actions_cost_report.py"
    spec = importlib.util.spec_from_file_location("actions_cost_report_overage", path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


def job(job_id, start, end):
    return {"id": job_id, "workflow_name": "CI", "name": f"job-{job_id}",
            "started_at": start, "completed_at": end, "conclusion": "success",
            "labels": ["ubuntu-24.04"], "runner_id": job_id}


def test_allowance_changes_only_overage_cost():
    m = module(); config = m.load_billing_config(ROOT / "ci" / "actions-billing.json")
    jobs = [job(1, "2026-08-13T00:00:00Z", "2026-08-13T00:01:01Z"),
            job(2, "2026-08-13T00:05:00Z", "2026-08-13T00:07:01Z")]
    below = m.build_report(jobs, config=config)
    assert below["totals"]["billed_minutes"] == 5
    assert below["totals"]["gross_equivalent_cost_usd"] == 0.03
    assert below["totals"]["approximate_overage_cost_usd"] == 0.0
    over = m.build_report(jobs, config=config, monthly_billed_minutes_before_period=1998)
    assert over["totals"]["overage_billed_minutes"] == 3
    assert over["totals"]["approximate_overage_cost_usd"] == 0.018


def test_allowance_allocation_uses_start_order():
    m = module(); config = m.load_billing_config(ROOT / "ci" / "actions-billing.json")
    later = job(2, "2026-08-13T00:05:00Z", "2026-08-13T00:06:01Z")
    earlier = job(1, "2026-08-13T00:00:00Z", "2026-08-13T00:01:01Z")
    report = m.build_report([later, earlier], config=config, monthly_billed_minutes_before_period=1999)
    by_job = report["by_workflow"]["CI"]["by_job"]
    assert by_job["job-1"]["overage_billed_minutes"] == 1
    assert by_job["job-2"]["overage_billed_minutes"] == 2


def test_month_boundary_accounting_fails_closed():
    m = module(); config = m.load_billing_config(ROOT / "ci" / "actions-billing.json")
    with pytest.raises(m.CostReportError, match="one UTC calendar month"):
        m.build_report([], config=config, since="2026-08-31T23:00:00Z", until="2026-09-01T00:00:00Z")
    with pytest.raises(m.CostReportError, match="monthly_billed_minutes_before_period"):
        m.build_report([], config=config, since="2026-08-13T00:00:00Z", until="2026-08-13T23:59:59Z")
