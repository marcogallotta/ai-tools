#!/usr/bin/env python3
"""Fail-closed helpers for Dish PR review discovery and exact-head Integration gates."""
from __future__ import annotations

import argparse
from datetime import datetime
from enum import Enum
import json
import re
import sys
from pathlib import Path
from typing import Any

from code_quality_admission import exact_head_admission

REQUIRED_CERTIFICATION_CONTEXT = "Dish / exact-head certification"
REQUIRED_CERTIFICATION_WORKFLOW_PATH = ".github/workflows/ci.yml"
# Compatibility names used by lifecycle/external-dependency records while Stage E converges terminology.
REQUIRED_ORDINARY_CI_CONTEXT = REQUIRED_CERTIFICATION_CONTEXT
REQUIRED_ORDINARY_CI_WORKFLOW_PATH = REQUIRED_CERTIFICATION_WORKFLOW_PATH
_RUN_TARGET_RE = re.compile(r"/actions/runs/(?P<run_id>[0-9]+)(?:/|$)")
_VERDICT_RE = re.compile(r"(?im)^\s*VERDICT:\s*(MERGE|BLOCK)\s*$")


class GateError(ValueError):
    """The supplied GitHub state does not satisfy a lifecycle gate."""


class GateDiagnosis(str, Enum):
    PASS = "PASS"
    PENDING = "PENDING"
    FAILED_REQUIRED_CI = "FAILED_REQUIRED_CI"
    EVIDENCE_MISSING_OR_STALE = "EVIDENCE_MISSING_OR_STALE"
    HEAD_MOVED = "HEAD_MOVED"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"


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


def _pr_number(pr: dict[str, Any]) -> int:
    value = pr.get("number")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise GateError("PR JSON is missing numeric number") from exc
    if number < 1:
        raise GateError("PR JSON number must be positive")
    return number


def pr_state(pr: dict[str, Any]) -> str:
    return _state(pr)


def pr_is_draft(pr: dict[str, Any]) -> bool:
    return _draft(pr)


def pr_head_sha(pr: dict[str, Any]) -> str:
    return _head_sha(pr)


def is_review_discoverable(
    pr: dict[str, Any], *, allow_draft: bool = False,
    comments: list[dict[str, Any]] | None = None,
    base_policy: bytes | str | None = None,
    head_policy: bytes | str | None = None,
) -> bool:
    if _state(pr) != "open":
        return False
    if _draft(pr) and not allow_draft:
        return False
    if comments is None:
        return True
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    admission = exact_head_admission(
        comments=comments,
        head=_head_sha(pr),
        target_base=str(base.get("sha") or pr.get("baseRefOid") or ""),
        pr_number=_pr_number(pr),
        base_policy=base_policy,
        head_policy=head_policy,
    )
    return admission.allowed


def review_verdict(body: Any) -> str | None:
    if not isinstance(body, str):
        return None
    match = _VERDICT_RE.search(body)
    return match.group(1) if match else None


def latest_exact_head_review(
    reviews: list[dict[str, Any]], *, reviewed_head: str
) -> dict[str, Any] | None:
    candidates: list[tuple[str, int, dict[str, Any]]] = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        if str(review.get("commit_id") or review.get("commitId") or "") != reviewed_head:
            continue
        if str(review.get("state", "")).upper() not in {"COMMENTED", "COMMENT"}:
            continue
        verdict = review_verdict(review.get("body"))
        if verdict is None:
            continue
        submitted = str(review.get("submitted_at") or review.get("submittedAt") or "")
        try:
            numeric_id = int(review.get("id"))
        except (TypeError, ValueError):
            numeric_id = 0
        normalized = dict(review)
        normalized["verdict"] = verdict
        candidates.append((submitted, numeric_id, normalized))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


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
        status for status in statuses
        if isinstance(status, dict) and status.get("context") == REQUIRED_CERTIFICATION_CONTEXT
    ]
    if not matches:
        raise GateError(f"required certification status {REQUIRED_CERTIFICATION_CONTEXT!r} is absent")
    return matches


def _status_run_id(status: dict[str, Any]) -> int | None:
    target_url = status.get("target_url")
    if not isinstance(target_url, str):
        return None
    match = _RUN_TARGET_RE.search(target_url)
    return int(match.group("run_id")) if match else None


def _run_int(run: dict[str, Any], field: str) -> int:
    try:
        return int(run.get(field))
    except (TypeError, ValueError) as exc:
        raise GateError(f"certification workflow run is missing numeric {field}") from exc


def _run_matches_pr(run: dict[str, Any], *, pr_number: int) -> bool:
    prs = run.get("pull_requests")
    if not isinstance(prs, list):
        return False
    for value in prs:
        if not isinstance(value, dict):
            continue
        try:
            if int(value.get("number")) == pr_number:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _newest_certification_attempt(
    workflow_runs: dict[str, Any], *, pr_number: int, reviewed_at: datetime
) -> tuple[dict[str, Any], datetime]:
    runs = workflow_runs.get("workflow_runs")
    if not isinstance(runs, list):
        raise GateError("workflow-runs JSON is missing workflow_runs[]")
    candidates: list[tuple[datetime, int, int, dict[str, Any]]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("path") != REQUIRED_CERTIFICATION_WORKFLOW_PATH:
            continue
        if run.get("event") != "pull_request_review":
            continue
        if not _run_matches_pr(run, pr_number=pr_number):
            continue
        started_at = _timestamp(
            run.get("run_started_at"), label="certification workflow run_started_at"
        )
        if started_at < reviewed_at:
            continue
        candidates.append((started_at, _run_int(run, "id"), _run_int(run, "run_attempt"), run))
    if not candidates:
        raise GateError(
            f"no certification pull_request_review workflow attempt exists for PR #{pr_number} "
            "at or after the formal Review"
        )
    started_at, _, _, run = max(candidates, key=lambda item: item[:3])
    return run, started_at


def _required_status_for_attempt(
    combined_status: dict[str, Any], *, run_id: int, freshness_after: datetime
) -> dict[str, Any]:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for status in _required_statuses(combined_status):
        if _status_run_id(status) != run_id:
            continue
        status_time = _timestamp(
            status.get("updated_at") or status.get("created_at"),
            label="required certification status timestamp",
        )
        if status_time <= freshness_after:
            continue
        candidates.append((status_time, status))
    if not candidates:
        raise GateError(
            "required certification status for the newest workflow attempt is absent or stale"
        )
    return max(candidates, key=lambda item: item[0])[1]


def _diagnosis_result(
    state: GateDiagnosis, *, current_head: str, reviewed_head: str, reason: str,
    certified_sha: str | None = None, run: dict[str, Any] | None = None,
    required_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "diagnosis": state.value,
        "current_head": current_head,
        "reviewed_head": reviewed_head,
        "certified_sha": certified_sha,
        "required_status_context": REQUIRED_CERTIFICATION_CONTEXT,
        "reason": reason,
    }
    if run is not None:
        result.update({
            "required_workflow_run_id": _run_int(run, "id"),
            "required_workflow_run_attempt": _run_int(run, "run_attempt"),
            "required_workflow_run_started_at": run.get("run_started_at"),
            "required_workflow_status": str(run.get("status", "")).lower(),
            "required_workflow_conclusion": str(run.get("conclusion") or "").lower(),
        })
    if required_status is not None:
        result.update({
            "required_status_state": str(required_status.get("state", "")).lower(),
            "target_url": required_status.get("target_url"),
        })
    return result


def diagnose_integration_gate(
    pr: dict[str, Any], *, reviewed_head: str, reviewed_at: str,
    combined_status: dict[str, Any], workflow_runs: dict[str, Any],
) -> dict[str, Any]:
    """Classify exact-head selector certification relative to the formal Review generation."""
    current_head = _head_sha(pr)
    if current_head != reviewed_head:
        return _diagnosis_result(
            GateDiagnosis.HEAD_MOVED,
            current_head=current_head,
            reviewed_head=reviewed_head,
            reason=f"PR head moved: reviewed {reviewed_head}, current {current_head}; re-review is required",
        )
    if _state(pr) != "open":
        return _diagnosis_result(
            GateDiagnosis.EVIDENCE_MISSING_OR_STALE,
            current_head=current_head, reviewed_head=reviewed_head,
            reason=f"PR state is {_state(pr)!r}, expected 'open'",
        )
    if _draft(pr):
        return _diagnosis_result(
            GateDiagnosis.EVIDENCE_MISSING_OR_STALE,
            current_head=current_head, reviewed_head=reviewed_head,
            reason="PR is draft; ordinary integration requires review-ready state",
        )
    status_sha = str(combined_status.get("sha", ""))
    if status_sha != reviewed_head:
        return _diagnosis_result(
            GateDiagnosis.EVIDENCE_MISSING_OR_STALE,
            current_head=current_head, reviewed_head=reviewed_head,
            certified_sha=status_sha or None,
            reason=f"required certification evidence is for {status_sha or '<missing>'}, not reviewed head {reviewed_head}",
        )

    review_time = _timestamp(reviewed_at, label="formal Review submitted_at")
    try:
        newest_run, started_at = _newest_certification_attempt(
            workflow_runs, pr_number=_pr_number(pr), reviewed_at=review_time
        )
    except GateError as exc:
        return _diagnosis_result(
            GateDiagnosis.EVIDENCE_MISSING_OR_STALE,
            current_head=current_head, reviewed_head=reviewed_head,
            certified_sha=status_sha, reason=str(exc),
        )

    run_id = _run_int(newest_run, "id")
    attempt = _run_int(newest_run, "run_attempt")
    run_status = str(newest_run.get("status", "")).lower()
    conclusion = str(newest_run.get("conclusion") or "").lower()
    if run_status != "completed":
        return _diagnosis_result(
            GateDiagnosis.PENDING,
            current_head=current_head, reviewed_head=reviewed_head, certified_sha=status_sha,
            run=newest_run,
            reason=f"newest certification workflow attempt {run_id}/{attempt} is {run_status or '<missing>'}, expected 'completed'",
        )
    if conclusion != "success":
        return _diagnosis_result(
            GateDiagnosis.FAILED_REQUIRED_CI,
            current_head=current_head, reviewed_head=reviewed_head, certified_sha=status_sha,
            run=newest_run,
            reason=f"newest certification workflow attempt {run_id}/{attempt} concluded {conclusion or '<missing>'}, expected 'success'",
        )

    try:
        required = _required_status_for_attempt(
            combined_status,
            run_id=run_id,
            freshness_after=max(review_time, started_at),
        )
    except GateError as exc:
        return _diagnosis_result(
            GateDiagnosis.EVIDENCE_MISSING_OR_STALE,
            current_head=current_head, reviewed_head=reviewed_head, certified_sha=status_sha,
            run=newest_run, reason=str(exc),
        )
    state = str(required.get("state", "")).lower()
    if state == "pending":
        return _diagnosis_result(
            GateDiagnosis.PENDING,
            current_head=current_head, reviewed_head=reviewed_head, certified_sha=status_sha,
            run=newest_run, required_status=required,
            reason="required certification status for newest workflow attempt is pending, expected 'success'",
        )
    if state != "success":
        return _diagnosis_result(
            GateDiagnosis.FAILED_REQUIRED_CI,
            current_head=current_head, reviewed_head=reviewed_head, certified_sha=status_sha,
            run=newest_run, required_status=required,
            reason=f"required certification status for newest workflow attempt is {state or '<missing>'}, expected 'success'",
        )
    return _diagnosis_result(
        GateDiagnosis.PASS,
        current_head=current_head, reviewed_head=reviewed_head, certified_sha=status_sha,
        run=newest_run, required_status=required,
        reason="exact reviewed head has fresh successful selector-driven certification",
    )


def evaluate_integration_gate(
    pr: dict[str, Any], *, reviewed_head: str, reviewed_at: str,
    combined_status: dict[str, Any], workflow_runs: dict[str, Any],
) -> dict[str, Any]:
    diagnosis = diagnose_integration_gate(
        pr,
        reviewed_head=reviewed_head,
        reviewed_at=reviewed_at,
        combined_status=combined_status,
        workflow_runs=workflow_runs,
    )
    if diagnosis["diagnosis"] != GateDiagnosis.PASS.value:
        raise GateError(str(diagnosis["reason"]))
    return {"ok": True, **diagnosis}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr_gate")
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review-ready", help="test ordinary Review discoverability")
    review.add_argument("--pr-json", required=True)
    review.add_argument("--allow-draft", action="store_true")
    review.add_argument("--comments-json", required=True)
    review.add_argument("--base-policy", required=True)
    review.add_argument("--head-policy", required=True)
    integration = sub.add_parser("integration", help="verify exact reviewed head certification")
    integration.add_argument("--pr-json", required=True)
    integration.add_argument("--reviewed-head", required=True)
    integration.add_argument("--reviewed-at", required=True)
    integration.add_argument("--status-json", required=True)
    integration.add_argument("--runs-json", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        pr = _load_json(args.pr_json)
        if args.command == "review-ready":
            loaded = json.loads(Path(args.comments_json).read_text(encoding="utf-8"))
            comments = loaded if isinstance(loaded, list) else loaded.get("comments")
            if not isinstance(comments, list):
                raise GateError("comments JSON must be a list or contain comments[]")
            result = {
                "discoverable": is_review_discoverable(
                    pr,
                    allow_draft=args.allow_draft,
                    comments=comments,
                    base_policy=Path(args.base_policy).read_bytes(),
                    head_policy=Path(args.head_policy).read_bytes(),
                ),
                "draft": _draft(pr), "head_sha": _head_sha(pr), "state": _state(pr),
            }
            print(json.dumps(result, sort_keys=True))
            return 0 if result["discoverable"] else 3
        result = evaluate_integration_gate(
            pr,
            reviewed_head=args.reviewed_head,
            reviewed_at=args.reviewed_at,
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
