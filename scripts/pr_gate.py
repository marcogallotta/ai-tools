#!/usr/bin/env python3
"""Fail-closed helpers for Dish PR review discovery and exact-head integration gates."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any

REQUIRED_ORDINARY_CI_CONTEXT = "Dish / required ordinary CI"
REQUIRED_ORDINARY_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
_RUN_TARGET_RE = re.compile(r"/actions/runs/(?P<run_id>[0-9]+)(?:/|$)")


class GateError(ValueError):
    """The supplied GitHub state does not satisfy a lifecycle gate."""


def _load_json(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"could not read JSON {path!r}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError(f"expected a JSON object in {path!r}")
    return value


def _state(pr: dict[str, Any]) -> str:
    return str(pr.get("state", "")).lower()


def _draft(pr: dict[str, Any]) -> bool:
    if "draft" in pr:
        return bool(pr["draft"])
    if "isDraft" in pr:
        return bool(pr["isDraft"])
    raise GateError("PR JSON is missing draft/isDraft")


def _head_sha(pr: dict[str, Any]) -> str:
    head = pr.get("head")
    if isinstance(head, dict) and head.get("sha"):
        return str(head["sha"])
    if pr.get("headRefOid"):
        return str(pr["headRefOid"])
    raise GateError("PR JSON is missing head.sha/headRefOid")


def is_review_discoverable(pr: dict[str, Any], *, allow_draft: bool = False) -> bool:
    """Return whether a PR belongs in ordinary Review discovery."""
    if _state(pr) != "open":
        return False
    if _draft(pr) and not allow_draft:
        return False
    return True


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise GateError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError(f"{label} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise GateError(f"{label} must include a timezone: {value!r}")
    return parsed


def _required_statuses(combined_status: dict[str, Any]) -> list[dict[str, Any]]:
    statuses = combined_status.get("statuses")
    if not isinstance(statuses, list):
        raise GateError("combined status JSON is missing statuses[]")
    matches = [
        status
        for status in statuses
        if isinstance(status, dict) and status.get("context") == REQUIRED_ORDINARY_CI_CONTEXT
    ]
    if not matches:
        raise GateError(
            f"required ordinary CI status {REQUIRED_ORDINARY_CI_CONTEXT!r} is absent"
        )
    return matches


def _status_run_id(status: dict[str, Any]) -> int | None:
    target_url = status.get("target_url")
    if not isinstance(target_url, str):
        return None
    match = _RUN_TARGET_RE.search(target_url)
    if not match:
        return None
    return int(match.group("run_id"))


def _run_int(run: dict[str, Any], field: str) -> int:
    value = run.get(field)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise GateError(f"ordinary CI workflow run is missing numeric {field}") from exc


def _newest_ordinary_ci_attempt(
    workflow_runs: dict[str, Any], *, reviewed_head: str
) -> tuple[dict[str, Any], datetime]:
    runs = workflow_runs.get("workflow_runs")
    if not isinstance(runs, list):
        raise GateError("workflow-runs JSON is missing workflow_runs[]")

    candidates: list[tuple[datetime, int, int, dict[str, Any]]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("path") != REQUIRED_ORDINARY_CI_WORKFLOW_PATH:
            continue
        if run.get("event") != "pull_request":
            continue
        if str(run.get("head_sha", "")) != reviewed_head:
            continue
        started_at = _timestamp(
            run.get("run_started_at"), label="ordinary CI workflow run_started_at"
        )
        candidates.append(
            (
                started_at,
                _run_int(run, "id"),
                _run_int(run, "run_attempt"),
                run,
            )
        )

    if not candidates:
        raise GateError(
            f"no ordinary CI pull_request workflow attempt exists for reviewed head {reviewed_head}"
        )

    started_at, _, _, run = max(candidates, key=lambda candidate: candidate[:3])
    return run, started_at


def _required_status_for_attempt(
    combined_status: dict[str, Any], *, run_id: int, attempt_started_at: datetime
) -> dict[str, Any]:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for status in _required_statuses(combined_status):
        if _status_run_id(status) != run_id:
            continue
        status_time = _timestamp(
            status.get("updated_at") or status.get("created_at"),
            label="required ordinary CI status timestamp",
        )
        # A GitHub Actions rerun reuses the workflow run ID. Requiring the status
        # write to be strictly newer than this attempt's start prevents an older
        # same-run success from certifying the rerun if the rerun never publishes.
        if status_time <= attempt_started_at:
            continue
        candidates.append((status_time, status))

    if not candidates:
        raise GateError(
            "required ordinary CI status for the newest workflow attempt is absent or stale"
        )
    return max(candidates, key=lambda candidate: candidate[0])[1]


def evaluate_integration_gate(
    pr: dict[str, Any],
    *,
    reviewed_head: str,
    combined_status: dict[str, Any],
    workflow_runs: dict[str, Any],
) -> dict[str, Any]:
    """Validate that Integration is acting on the exact reviewed, fully certified PR head."""
    if _state(pr) != "open":
        raise GateError(f"PR state is {_state(pr)!r}, expected 'open'")
    if _draft(pr):
        raise GateError("PR is draft; ordinary integration requires review-ready state")

    current_head = _head_sha(pr)
    if current_head != reviewed_head:
        raise GateError(
            f"PR head moved: reviewed {reviewed_head}, current {current_head}; re-review is required"
        )

    status_sha = str(combined_status.get("sha", ""))
    if status_sha != reviewed_head:
        raise GateError(
            f"required CI evidence is for {status_sha or '<missing>'}, not reviewed head {reviewed_head}"
        )

    newest_run, newest_started_at = _newest_ordinary_ci_attempt(
        workflow_runs, reviewed_head=reviewed_head
    )
    run_id = _run_int(newest_run, "id")
    run_attempt = _run_int(newest_run, "run_attempt")
    run_status = str(newest_run.get("status", "")).lower()
    run_conclusion = str(newest_run.get("conclusion") or "").lower()
    if run_status != "completed":
        raise GateError(
            f"newest ordinary CI workflow attempt {run_id}/{run_attempt} is {run_status or '<missing>'}, expected 'completed'"
        )
    if run_conclusion != "success":
        raise GateError(
            f"newest ordinary CI workflow attempt {run_id}/{run_attempt} concluded {run_conclusion or '<missing>'}, expected 'success'"
        )

    required = _required_status_for_attempt(
        combined_status,
        run_id=run_id,
        attempt_started_at=newest_started_at,
    )
    state = str(required.get("state", "")).lower()
    if state != "success":
        raise GateError(
            f"required ordinary CI status for newest workflow attempt is {state or '<missing>'}, expected 'success'"
        )

    return {
        "ok": True,
        "current_head": current_head,
        "reviewed_head": reviewed_head,
        "certified_sha": status_sha,
        "required_status_context": REQUIRED_ORDINARY_CI_CONTEXT,
        "required_status_state": state,
        "required_workflow_run_id": run_id,
        "required_workflow_run_attempt": run_attempt,
        "required_workflow_run_started_at": newest_run.get("run_started_at"),
        "required_workflow_conclusion": run_conclusion,
        "target_url": required.get("target_url"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr_gate")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review-ready", help="test ordinary Review discoverability")
    review.add_argument("--pr-json", required=True)
    review.add_argument(
        "--allow-draft",
        action="store_true",
        help="Marco-requested exceptional early review of a draft PR",
    )

    integration = sub.add_parser(
        "integration", help="verify exact reviewed head and required ordinary CI certification"
    )
    integration.add_argument("--pr-json", required=True)
    integration.add_argument("--reviewed-head", required=True)
    integration.add_argument("--status-json", required=True)
    integration.add_argument("--runs-json", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        pr = _load_json(args.pr_json)
        if args.command == "review-ready":
            result = {
                "discoverable": is_review_discoverable(pr, allow_draft=args.allow_draft),
                "draft": _draft(pr),
                "head_sha": _head_sha(pr),
                "state": _state(pr),
            }
            print(json.dumps(result, sort_keys=True))
            return 0 if result["discoverable"] else 3

        result = evaluate_integration_gate(
            pr,
            reviewed_head=args.reviewed_head,
            combined_status=_load_json(args.status_json),
            workflow_runs=_load_json(args.runs_json),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except GateError as exc:
        print(f"pr_gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
