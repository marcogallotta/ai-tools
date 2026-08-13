#!/usr/bin/env python3
"""Report GitHub Actions runtime, rounded billed minutes, and allowance-aware cost."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
    included = config.get("included_minutes_per_month")
    if isinstance(included, bool) or not isinstance(included, int) or included < 0:
        raise CostReportError("included_minutes_per_month must be a non-negative integer")
    rates = config.get("rates_usd_per_billed_minute")
    if not isinstance(rates, dict) or not rates:
        raise CostReportError("billing config must define rates_usd_per_billed_minute")
    for label, rate in rates.items():
        if not isinstance(label, str) or not isinstance(rate, (int, float)) or rate < 0:
            raise CostReportError("billing rates must map runner labels to non-negative numbers")
    return config


def _monthly_window(since: str | None, until: str | None) -> tuple[datetime, datetime, str]:
    if since is None or until is None:
        raise CostReportError(
            "allowance-aware cost requires a complete UTC calendar month period"
        )
    start = _parse_timestamp(since, field="since")
    inclusive_end = _parse_timestamp(until, field="until")
    canonical_start = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    if start != canonical_start:
        raise CostReportError("since must be the first instant of a UTC calendar month")
    if start.month == 12:
        next_month = datetime(start.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(start.year, start.month + 1, 1, tzinfo=timezone.utc)
    canonical_end = next_month - timedelta(seconds=1)
    if inclusive_end != canonical_end:
        raise CostReportError(
            "until must be the final second of the same UTC calendar month as since"
        )
    return start, next_month, f"{start.year:04d}-{start.month:02d}"


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


def build_report(
    jobs: Iterable[Mapping[str, object]],
    *,
    config: Mapping[str, object],
    since: str | None = None,
    until: str | None = None,
) -> dict[str, object]:
    period_start, period_end, billing_month = _monthly_window(since, until)
    included_minutes = config.get("included_minutes_per_month")
    if (
        isinstance(included_minutes, bool)
        or not isinstance(included_minutes, int)
        or included_minutes < 0
    ):
        raise CostReportError("included_minutes_per_month must be a non-negative integer")

    by_workflow: dict[str, dict[str, object]] = {}
    total_runtime = 0
    total_billed = 0
    total_gross_cost = 0.0
    total_jobs = 0
    cancelled_billed = 0
    billable_records: list[tuple[datetime, str, int, float]] = []

    workflow_acc: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "jobs": 0,
            "runtime_seconds": 0,
            "billed_minutes": 0,
            "gross_equivalent_cost_usd": 0.0,
            "by_job": defaultdict(
                lambda: {
                    "jobs": 0,
                    "runtime_seconds": 0,
                    "billed_minutes": 0,
                    "gross_equivalent_cost_usd": 0.0,
                }
            ),
        }
    )

    seen_ids: set[object] = set()
    for job in jobs:
        job_id = job.get("id")
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        workflow = str(job.get("workflow_name") or "<unknown workflow>")
        name = str(job.get("name") or "<unknown job>")
        seconds = runtime_seconds(job)
        minutes = billed_minutes(job)
        _, rate = _rate_for_job(job, config)
        gross_cost = minutes * rate

        if minutes:
            started = _parse_timestamp(job.get("started_at"), field="started_at")
            if not period_start <= started < period_end:
                raise CostReportError(
                    f"billable job {job_id} started outside billing month {billing_month}"
                )
            billable_records.append((started, str(job_id), minutes, rate))

        total_jobs += 1
        total_runtime += seconds
        total_billed += minutes
        total_gross_cost += gross_cost
        if job.get("conclusion") == "cancelled":
            cancelled_billed += minutes

        workflow_entry = workflow_acc[workflow]
        workflow_entry["jobs"] = int(workflow_entry["jobs"]) + 1
        workflow_entry["runtime_seconds"] = int(workflow_entry["runtime_seconds"]) + seconds
        workflow_entry["billed_minutes"] = int(workflow_entry["billed_minutes"]) + minutes
        workflow_entry["gross_equivalent_cost_usd"] = (
            float(workflow_entry["gross_equivalent_cost_usd"]) + gross_cost
        )
        jobs_entry = workflow_entry["by_job"]
        assert isinstance(jobs_entry, defaultdict)
        job_entry = jobs_entry[name]
        job_entry["jobs"] += 1
        job_entry["runtime_seconds"] += seconds
        job_entry["billed_minutes"] += minutes
        job_entry["gross_equivalent_cost_usd"] += gross_cost

    remaining_included = included_minutes
    overage_billed = 0
    overage_cost = 0.0
    for _, _, minutes, rate in sorted(
        billable_records, key=lambda item: (item[0], item[1])
    ):
        included_for_job = min(remaining_included, minutes)
        remaining_included -= included_for_job
        overage_for_job = minutes - included_for_job
        overage_billed += overage_for_job
        overage_cost += overage_for_job * rate

    for workflow in sorted(workflow_acc):
        entry = workflow_acc[workflow]
        jobs_entry = entry["by_job"]
        assert isinstance(jobs_entry, defaultdict)
        entry["by_job"] = {
            job_name: {
                **job_data,
                "gross_equivalent_cost_usd": round(
                    job_data["gross_equivalent_cost_usd"], 6
                ),
            }
            for job_name, job_data in sorted(jobs_entry.items())
        }
        entry["gross_equivalent_cost_usd"] = round(
            float(entry["gross_equivalent_cost_usd"]), 6
        )
        by_workflow[workflow] = entry

    included_consumed = min(total_billed, included_minutes)
    return {
        "schema": REPORT_SCHEMA,
        "period": {
            "billing_month_utc": billing_month,
            "since": since,
            "until": until,
        },
        "billing_config": {
            "job_rounding": config.get("job_rounding"),
            "included_minutes_per_month": included_minutes,
            "allowance_allocation": "chronological_job_start_utc",
            "rates_usd_per_billed_minute": config.get("rates_usd_per_billed_minute"),
        },
        "totals": {
            "jobs": total_jobs,
            "runtime_seconds": total_runtime,
            "billed_minutes": total_billed,
            "cancelled_billed_minutes": cancelled_billed,
            "included_minutes_consumed": included_consumed,
            "remaining_included_minutes": max(included_minutes - total_billed, 0),
            "overage_billed_minutes": overage_billed,
            "gross_equivalent_cost_usd": round(total_gross_cost, 6),
            "approximate_overage_cost_usd": round(overage_cost, 6),
        },
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
            query = urlencode(
                {
                    "status": "completed",
                    "created": created,
                    "per_page": 100,
                    "page": page,
                }
            )
            payload = self._get_json(
                f"https://api.github.com/repos/{self.repo}/actions/runs?{query}"
            )
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
            payload = self._get_json(
                f"https://api.github.com/repos/{self.repo}/actions/runs/{run_id}/jobs?{query}"
            )
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
    parser.add_argument(
        "--since",
        required=True,
        help="first instant of a UTC calendar month, e.g. 2026-08-01T00:00:00Z",
    )
    parser.add_argument(
        "--until",
        required=True,
        help="final second of the same UTC calendar month, e.g. 2026-08-31T23:59:59Z",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("ci/actions-billing.json"),
    )
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
        report = build_report(jobs, config=config, since=args.since, until=args.until)
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
