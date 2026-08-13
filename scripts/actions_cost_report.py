#!/usr/bin/env python3
"""Report GitHub Actions runtime, rounded billed minutes, and allowance-aware cost."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlencode

_CORE_PATH = Path(__file__).with_name("actions_cost_report_core.py")
_CORE_SPEC = importlib.util.spec_from_file_location("dish_actions_cost_report_core", _CORE_PATH)
if _CORE_SPEC is None or _CORE_SPEC.loader is None:  # pragma: no cover - import invariant
    raise RuntimeError(f"cannot load cost-report core from {_CORE_PATH}")
_core = importlib.util.module_from_spec(_CORE_SPEC)
_CORE_SPEC.loader.exec_module(_core)

REPORT_SCHEMA = _core.REPORT_SCHEMA
CONFIG_SCHEMA = _core.CONFIG_SCHEMA
API_VERSION = _core.API_VERSION
CostReportError = _core.CostReportError
runtime_seconds = _core.runtime_seconds
billed_minutes = _core.billed_minutes
load_billing_config = _core.load_billing_config
_rate_for_job = _core._rate_for_job
_monthly_window = _core._monthly_window

WORKFLOW_RUN_LOOKBACK = timedelta(days=35)
GITHUB_SEARCH_RESULT_CAP = 1000


def build_report(
    jobs: Iterable[Mapping[str, object]],
    *,
    config: Mapping[str, object],
    since: str | None = None,
    until: str | None = None,
) -> dict[str, object]:
    """Attribute expanded run evidence to the requested month by job start time."""
    period_start, period_end, _ = _monthly_window(since, until)
    attributed: list[Mapping[str, object]] = []
    for job in jobs:
        started_value = job.get("started_at")
        if not started_value:
            if job.get("conclusion") == "skipped" or not job.get("runner_id"):
                continue
            raise CostReportError(f"billable job {job.get('id')} is missing started_at")
        started = _core._parse_timestamp(started_value, field="started_at")
        if period_start <= started < period_end:
            attributed.append(job)
    return _core.build_report(
        attributed,
        config=config,
        since=since,
        until=until,
    )


class GitHubClient(_core.GitHubClient):
    @staticmethod
    def _github_timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

    def _runs_created_between(
        self, start: datetime, end: datetime
    ) -> list[Mapping[str, object]]:
        if start >= end:
            return []
        inclusive_end = end - timedelta(seconds=1)
        created = f"{self._github_timestamp(start)}..{self._github_timestamp(inclusive_end)}"

        def fetch_page(page: int) -> dict[str, object]:
            query = urlencode({"created": created, "per_page": 100, "page": page})
            return self._get_json(
                f"https://api.github.com/repos/{self.repo}/actions/runs?{query}"
            )

        first = fetch_page(1)
        total_count = first.get("total_count")
        if (
            isinstance(total_count, bool)
            or not isinstance(total_count, int)
            or total_count < 0
        ):
            raise CostReportError("workflow runs response is missing valid total_count")
        if total_count > GITHUB_SEARCH_RESULT_CAP:
            seconds = int((end - start).total_seconds())
            if seconds <= 1:
                raise CostReportError(
                    "workflow run search exceeds GitHub's 1,000-result cap within one second"
                )
            midpoint = start + timedelta(seconds=seconds // 2)
            return self._runs_created_between(
                start, midpoint
            ) + self._runs_created_between(midpoint, end)

        batch = first.get("workflow_runs")
        if not isinstance(batch, list):
            raise CostReportError("workflow runs response is missing workflow_runs")
        runs = [item for item in batch if isinstance(item, dict)]
        pages = math.ceil(total_count / 100)
        for page in range(2, pages + 1):
            payload = fetch_page(page)
            batch = payload.get("workflow_runs")
            if not isinstance(batch, list):
                raise CostReportError("workflow runs response is missing workflow_runs")
            runs.extend(item for item in batch if isinstance(item, dict))
        if len(runs) != total_count:
            raise CostReportError(
                f"workflow run search returned {len(runs)} records but reported {total_count}"
            )
        return runs

    def runs_for_billing_month(
        self, *, since: str, until: str
    ) -> list[Mapping[str, object]]:
        period_start, period_end, _ = _monthly_window(since, until)
        collection_start = period_start - WORKFLOW_RUN_LOOKBACK
        runs = self._runs_created_between(collection_start, period_end)
        deduplicated: dict[object, Mapping[str, object]] = {}
        for run in runs:
            run_id = run.get("id")
            if run_id is None:
                raise CostReportError("workflow run is missing id")
            deduplicated[run_id] = run
        return list(deduplicated.values())


def _parser():
    return _core._parser()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_billing_config(args.config)
        token = os.environ.get(args.token_env, "")
        client = GitHubClient(repo=args.repo or "", token=token)
        runs = client.runs_for_billing_month(since=args.since, until=args.until)
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
