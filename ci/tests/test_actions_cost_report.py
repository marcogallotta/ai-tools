import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts" / "actions_cost_report.py"
CONFIG = ROOT / "ci" / "actions-billing.json"
FIXTURE = ROOT / "ci" / "fixtures" / "actions-run-31697885898-jobs.json"


def module():
    spec = importlib.util.spec_from_file_location("actions_cost_report", MODULE)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


def job(seconds, *, conclusion="success", runner_id=1):
    return {"id": 1, "workflow_name": "CI", "name": "example",
            "started_at": "2026-08-13T00:00:00Z",
            "completed_at": f"2026-08-13T00:{seconds // 60:02d}:{seconds % 60:02d}Z",
            "conclusion": conclusion, "labels": ["ubuntu-24.04"], "runner_id": runner_id}


def test_per_job_rounding():
    m = module()
    assert [m.billed_minutes(job(n)) for n in (1, 59, 60, 61)] == [1, 1, 1, 2]
    assert m.billed_minutes(job(0)) == 1
    assert m.billed_minutes(job(0, conclusion="skipped", runner_id=None)) == 0


def test_historical_ci_preserves_23_billed_minutes_without_false_overage():
    m = module(); config = m.load_billing_config(CONFIG)
    fixture = json.loads(FIXTURE.read_text())
    report = m.build_report(fixture["jobs"], config=config)
    assert fixture["source"]["run_id"] == 31697885898
    assert report["totals"]["runtime_seconds"] == 1074
    assert report["totals"]["billed_minutes"] == 23
    assert report["totals"]["gross_equivalent_cost_usd"] == 0.138
    assert report["totals"]["overage_billed_minutes"] == 0
    assert report["totals"]["approximate_overage_cost_usd"] == 0.0


def test_cancelled_minutes_and_duplicate_ids():
    m = module(); config = m.load_billing_config(CONFIG); item = job(61, conclusion="cancelled")
    report = m.build_report([item, dict(item)], config=config)
    assert report["totals"]["jobs"] == 1
    assert report["totals"]["billed_minutes"] == 2
    assert report["totals"]["cancelled_billed_minutes"] == 2


def test_unknown_runner_rate_fails_closed():
    m = module(); config = m.load_billing_config(CONFIG); item = job(30); item["labels"] = ["windows-2025"]
    with pytest.raises(m.CostReportError, match="exactly one configured billing label"):
        m.build_report([item], config=config)
