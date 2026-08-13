#!/usr/bin/env python3
"""Report GitHub Actions runtime, rounded billed minutes, and allowance-aware cost."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPORT_SCHEMA = "dish-github-actions-cost-report-v1"
CONFIG_SCHEMA = "dish-github-actions-billing-config-v1"
API_VERSION = "2022-11-28"


class CostReportError(ValueError):
    """Raised for malformed billing inputs or incomplete GitHub evidence."""


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CostReportError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CostReportError(f"invalid {field}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise CostReportError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def runtime_seconds(job: Mapping[str, object]) -> int:
    if job.get("conclusion") == "skipped" or not job.get("runner_id"):
        return 0
    started = _parse_timestamp(job.get("started_at"), field="started_at")
    completed = _parse_timestamp(job.get("completed_at"), field="completed_at")
    seconds = int((completed - started).total_seconds())
    if seconds < 0:
        raise CostReportError(f"job {job.get('id')} completed before it started")
    return seconds


def billed_minutes(job: Mapping[str, object]) -> int:
    seconds = runtime_seconds(job)
    if seconds == 0:
        if job.get("conclusion") == "skipped" or not job.get("runner_id"):
            return 0
        return 1
    return max(1, math.ceil(seconds / 60))


def load_billing_config(path: Path) -> dict[str, object]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CostReportError(f"cannot read billing config {path}: {exc}") from exc
    if not isinstance(config, dict) or config.get("schema") != CONFIG_SCHEMA:
        raise CostReportError(f"billing config schema must be {CONFIG_SCHEMA!r}")
    rates = config.get("rates_usd_per_billed_minute")
    if not isinstance(rates, dict) or not rates:
        raise CostReportError("billing config must define rates_usd_per_billed_minute")
    for label, rate in rates.items():
        if not isinstance(label, str) or not isinstance(rate, (int, float)) or rate < 0:
            raise CostReportError("billing rates must map runner labels to non-negative numbers")
    allowance = config.get("included_minutes_per_month")
    if not isinstance(allowance, int) or isinstance(allowance, bool) or allowance < 0:
        raise CostReportError("included_minutes_per_month must be a non-negative integer")
    return config


def _rate_for_job(job: Mapping[str, object], config: Mapping[str, object]) -> tuple[str | None, float]:
    if billed_minutes(job) == 0:
        return None, 0.0
    labels = job.get("labels")
    if not isinstance(labels, list):
        raise CostReportError(f"job {job.get('id')} is missing runner labels")
    rates = config["rates_usd_per_billed_minute"]
    assert isinstance(rates, dict)
    matches = [(label, float(rates[label])) for label in labels if label in rates]
    if len(matches) != 1:
        raise CostReportError(
            f"job {job.get('id')} must match exactly one configured billing label; labels={labels!r}"
        )
    return matches[0]


def _period(
    since: str | None,
    until: str | None,
    monthly_billed_minutes_before_period: int | None,
) -> tuple[dict[str, object], int]:
    if (since is None) != (until is None):
        raise CostReportError("since and until must be supplied together")
    if monthly_billed_minutes_before_period is not None and (
        not isinstance(monthly_billed_minutes_before_period, int)
        or isinstance(monthly_billed_minutes_before_period, bool)
        or monthly_billed_minutes_before_period < 0
    ):
        raise CostReportError("monthly_billed_minutes_before_period must be a non-negative integer")
    if since is None:
        return {"since": None, "until": None, "billing_month_utc": None}, monthly_billed_minutes_before_period or 0

    start = _parse_timestamp(since, field="since")
    end = _parse_timestamp(until, field="until")
    if end < start:
        raise CostReportError("until must not precede since")
    if (start.year, start.month) != (end.year, end.month):
        raise CostReportError("cost report period must stay within one UTC calendar month")
    month_start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start != month_start and monthly_billed_minutes_before_period is None:
        raise CostReportError(
            "non-month-start periods require monthly_billed_minutes_before_period for truthful overage accounting"
        )
    return {
        "since": since,
        "until": until,
        "billing_month_utc": f"{start.year:04d}-{start.month:02d}",
    }, monthly_billed_minutes_before_period or 0


def build_report(
    jobs: Iterable[Mapping[str, object]],
    *,
    config: Mapping[str, object],
    since: str | None = None,
    until: str | None = None,
    monthly_billed_minutes_before_period: int | None = None,
) -> dict[str, object]:
    period, prior_billed = _period(since, until, monthly_billed_minutes_before_period)
    allowance = int(config.get("included_minutes_per_month", 0))

    seen_ids: set[object] = set()
    prepared: list[dict[str, object]] = []
    for job in jobs:
        job_id = job.get("id")
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        seconds = runtime_seconds(job)
        minutes = billed_minutes(job)
        label, rate = _rate_for_job(job, config)
        started = None if minutes == 0 else _parse_timestamp(job.get("started_at"), field="started_at")
        prepared.append({
            "job": job,
            "job_id": job_id,
            "workflow": str(job.get("workflow_name") or "<unknown workflow>"),
            "name": str(job.get("name") or "<unknown job>"),
            "runtime_seconds": seconds,
            "billed_minutes": minutes,
            "billing_label": label,
            "rate": rate,
            "started": started,
            "gross_equivalent_cost_usd": minutes * rate,
            "overage_billed_minutes": 0,
            "approximate_overage_cost_usd": 0.0,
        })

    remaining_allowance = max(0, allowance - prior_billed)
    chargeable = [item for item in prepared if int(item["billed_minutes"]) > 0]
    chargeable.sort(key=lambda item: (item["started"], str(item["job_id"])))
    for item in chargeable:
        minutes = int(item["billed_minutes"])
        included_here = min(minutes, remaining_allowance)
        overage = minutes - included_here
        remaining_allowance -= included_here
        item["overage_billed_minutes"] = overage
        item["approximate_overage_cost_usd"] = overage * float(item["rate"])

    workflow_acc: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "jobs": 0,
            "runtime_seconds": 0,
            "billed_minutes": 0,
            "cancelled_billed_minutes": 0,
            "gross_equivalent_cost_usd": 0.0,
            "overage_billed_minutes": 0,
            "approximate_overage_cost_usd": 0.0,
            "by_job": defaultdict(
                lambda: {
                    "jobs": 0,
                    "runtime_seconds": 0,
                    "billed_minutes": 0,
                    "cancelled_billed_minutes": 0,
                    "gross_equivalent_cost_usd": 0.0,
                    "overage_billed_minutes": 0,
                    "approximate_overage_cost_usd": 0.0,
                }
            ),
        }
    )
    totals = {
        "jobs": 0,
        "runtime_seconds": 0,
        "billed_minutes": 0,
        "cancelled_billed_minutes": 0,
        "gross_equivalent_cost_usd": 0.0,
        "overage_billed_minutes": 0,
        "approximate_overage_cost_usd": 0.0,
    }

    for item in prepared:
        job = item["job"]
        assert isinstance(job, Mapping)
        cancelled = int(item["billed_minutes"]) if job.get("conclusion") == "cancelled" else 0
        metrics = {
            "jobs": 1,
            "runtime_seconds": int(item["runtime_seconds"]),
            "billed_minutes": int(item["billed_minutes"]),
            "cancelled_billed_minutes": cancelled,
            "gross_equivalent_cost_usd": float(item["gross_equivalent_cost_usd"]),
            "overage_billed_minutes": int(item["overage_billed_minutes"]),
            "approximate_overage_cost_usd": float(item["approximate_overage_cost_usd"]),
        }
        for key, value in metrics.items():
            totals[key] += value
        workflow_entry = workflow_acc[str(item["workflow"])]
        job_entry = workflow_entry["by_job"][str(item["name"])]
        for target in (workflow_entry, job_entry):
            for key, value in metrics.items():
                target[key] += value

    def _rounded(entry: Mapping[str, object]) -> dict[str, object]:
        return {
            **entry,
            "gross_equivalent_cost_usd": round(float(entry["gross_equivalent_cost_usd"]), 6),
            "approximate_overage_cost_usd": round(float(entry["approximate_overage_cost_usd"]), 6),
        }

    by_workflow: dict[str, dict[str, object]] = {}
    for workflow in sorted(workflow_acc):
        entry = workflow_acc[workflow]
        jobs_entry = entry.pop("by_job")
        assert isinstance(jobs_entry, defaultdict)
        rendered = _rounded(entry)
        rendered["by_job"] = {
            job_name: _rounded(job_data)
            for job_name, job_data in sorted(jobs_entry.items())
        }
        by_workflow[workflow] = rendered

    total_billed = int(totals["billed_minutes"])
    prior_overage = max(0, prior_billed - allowance)
    ending_overage = max(0, prior_billed + total_billed - allowance)
    assert int(totals["overage_billed_minutes"]) == ending_overage - prior_overage

    totals = _rounded(totals)
    return {
        "schema": REPORT_SCHEMA,
        "period": period,
        "billing_config": {
            "job_rounding": config.get("job_rounding"),
            "included_minutes_per_month": allowance,
            "rates_usd_per_billed_minute": config.get("rates_usd_per_billed_minute"),
        },
        "allowance": {
            "monthly_billed_minutes_before_period": prior_billed,
            "included_minutes_remaining_before_period": max(0, allowance - prior_billed),
            "included_minutes_remaining_after_period": max(0, allowance - prior_billed - total_billed),
            "allocation_rule": "started_at_then_job_id",
        },
        "totals": totals,
        "by_workflow": by_workflow,
    }


class GitHubClient:
    def __init__(self, *, repo: str, token: str) -> None:
        if repo.count("/") != 1:
            raise CostReportError("repo must be owner/name")
        if not token:
            raise CostReportError("GitHub token is required")
        self.repo = repo
        self.token = token

    def _get_json(self, url: str) -> dict[str, object]:
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "dish-actions-cost-report",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed GitHub API origin
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise CostReportError(f"GitHub API request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise CostReportError("GitHub API response must be an object")
        return payload

    def completed_runs(self, *, since: str, until: str) -> list[Mapping[str, object]]:
        runs: list[Mapping[str, object]] = []
        page = 1
        created = f"{since}..{until}"
        while True:
            query = urlencode({"status": "completed", "created": created, "per_page": 100, "page": page})
            payload = self._get_json(f"https://api.github.com/repos/{self.repo}/actions/runs?{query}")
            batch = payload.get("workflow_runs")
            if not isinstance(batch, list):
                raise CostReportError("workflow runs response is missing workflow_runs")
            runs.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                return runs
            page += 1

    def jobs_for_run(self, run_id: object) -> list[Mapping[str, object]]:
        jobs: list[Mapping[str, object]] = []
        page = 1
        while True:
            query = urlencode({"filter": "all", "per_page": 100, "page": page})
            payload = self._get_json(f"https://api.github.com/repos/{self.repo}/actions/runs/{run_id}/jobs?{query}")
            batch = payload.get("jobs")
            if not isinstance(batch, list):
                raise CostReportError(f"jobs response for run {run_id} is missing jobs")
            jobs.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                return jobs
            page += 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="actions_cost_report")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--since", required=True, help="inclusive ISO timestamp; report must stay within one UTC month")
    parser.add_argument("--until", required=True, help="inclusive ISO timestamp; report must stay within one UTC month")
    parser.add_argument("--monthly-billed-before-period", type=int)
    parser.add_argument("--config", type=Path, default=Path("ci/actions-billing.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--token-env", default="GH_TOKEN")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_billing_config(args.config)
        token = os.environ.get(args.token_env, "")
        client = GitHubClient(repo=args.repo or "", token=token)
        runs = client.completed_runs(since=args.since, until=args.until)
        jobs = [job for run in runs for job in client.jobs_for_run(run.get("id"))]
        report = build_report(
            jobs,
            config=config,
            since=args.since,
            until=args.until,
            monthly_billed_minutes_before_period=args.monthly_billed_before_period,
        )
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        return 0
    except CostReportError as exc:
        print(f"actions_cost_report: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
