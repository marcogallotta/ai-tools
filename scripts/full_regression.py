#!/usr/bin/env python3
"""Periodic full-regression evidence, triage, and selector-miss contracts.

This module deliberately does not own certification lane semantics.  The workflow
supplies the concrete command for each execution group; this wrapper only records
an all-groups diagnostic run and validates the durable triage feedback contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree

EVIDENCE_SCHEMA = "dish-full-regression-v1"
TRIAGE_SCHEMA = "dish-full-regression-triage-v1"
RUN_STATE_SCHEMA = "dish-full-regression-run-state-v1"
COMPONENT_SCHEMA = "dish-full-regression-component-v1"
FAILURE_SCHEMA = "dish-full-regression-failure-v1"

LANES = (
    "python-control-plane",
    "frontend-static-tooling",
    "native-postgresql",
    "browser-acceptance",
)
CLASSIFICATIONS = (
    "related regression",
    "unrelated baseline",
    "environment-infrastructure",
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ContractError(ValueError):
    """Raised when durable evidence violates the repository contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_sha(value: str, label: str) -> str:
    value = str(value).strip().lower()
    if not _SHA_RE.fullmatch(value):
        raise ContractError(f"{label} must be a 40-character lowercase Git SHA")
    return value


def _optional_sha(value: str | None, label: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _require_sha(str(value), label)


def _github_output(path: Path | None, values: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif value is None:
                value = ""
            handle.write(f"{key}={value}\n")


def decide_run(
    *,
    runs_payload: Mapping[str, Any],
    main_sha: str,
    event: str,
    current_run_id: str,
) -> dict[str, Any]:
    """Decide scheduled dedupe while preserving manual force-run semantics."""
    main_sha = _require_sha(main_sha, "main_sha")
    previous: Mapping[str, Any] | None = None
    equivalent_success: Mapping[str, Any] | None = None
    for run in runs_payload.get("workflow_runs", []):
        if str(run.get("id", "")) == str(current_run_id):
            continue
        if run.get("status") != "completed":
            continue
        if previous is None:
            previous = run
        if (
            run.get("conclusion") == "success"
            and str(run.get("head_sha", "")).lower() == main_sha
            and equivalent_success is None
        ):
            equivalent_success = run

    should_run = not (event == "schedule" and equivalent_success is not None)
    previous_sha = None
    previous_run_id = None
    previous_conclusion = None
    if previous is not None:
        candidate = str(previous.get("head_sha", "")).lower()
        if _SHA_RE.fullmatch(candidate):
            previous_sha = candidate
        previous_run_id = str(previous.get("id", "")) or None
        previous_conclusion = str(previous.get("conclusion", "")) or None

    return {
        "should_run": should_run,
        "main_sha": main_sha,
        "previous_main_sha": previous_sha,
        "previous_run_id": previous_run_id,
        "previous_run_conclusion": previous_conclusion,
        "equivalent_success_run_id": (
            str(equivalent_success.get("id")) if equivalent_success is not None else None
        ),
        "dedupe_reason": (
            "unchanged main already has a successful completed full regression"
            if not should_run
            else None
        ),
    }


def begin_run(
    *,
    output_dir: Path,
    main_sha: str,
    previous_main_sha: str | None,
    previous_run_id: str | None,
    run_id: str,
    run_attempt: str,
    event: str,
    reason: str,
    workflow_ref: str,
    started_at_epoch: float | None,
) -> dict[str, Any]:
    main_sha = _require_sha(main_sha, "main_sha")
    previous_main_sha = _optional_sha(previous_main_sha, "previous_main_sha")
    started_epoch = started_at_epoch if started_at_epoch is not None else time.time()
    started_at = datetime.fromtimestamp(started_epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    range_expression = (
        f"{previous_main_sha}..{main_sha}" if previous_main_sha is not None else None
    )
    payload = {
        "schema": RUN_STATE_SCHEMA,
        "main_sha": main_sha,
        "previous_main_sha": previous_main_sha,
        "commit_range": range_expression,
        "previous_run_id": previous_run_id or None,
        "run_id": str(run_id),
        "run_attempt": str(run_attempt),
        "event": event,
        "manual_reason": reason.strip() or None,
        "workflow_ref": workflow_ref,
        "started_at": started_at,
        "started_at_epoch": started_epoch,
    }
    _write_json(output_dir / "run-state.json", payload)
    return payload


def _component_path(output_dir: Path, kind: str, name: str) -> Path:
    safe_name = re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-")
    return output_dir / "components" / f"{kind}-{safe_name}.json"


def _failure_identity(*, kind: str, component: str, source: str, invariant: str) -> str:
    source_slug = re.sub(r"[^a-z0-9_.-]+", "-", source.lower()).strip("-") or "unknown"
    digest = hashlib.sha256(
        "\0".join((kind, component, source, invariant)).encode("utf-8")
    ).hexdigest()[:16]
    return f"{kind}:{component}:{source_slug}:{digest}"


def record_failure(
    *,
    output_dir: Path,
    kind: str,
    component: str,
    source: str,
    invariant: str,
    failure_kind: str,
    detail: str | None = None,
) -> dict[str, Any]:
    if kind not in {"lane", "phase"}:
        raise ContractError(f"unsupported failure kind: {kind}")
    if not component.strip() or not source.strip() or not invariant.strip() or not failure_kind.strip():
        raise ContractError("failure component/source/invariant/failure_kind are required")
    failure_id = _failure_identity(
        kind=kind, component=component, source=source, invariant=invariant
    )
    payload = {
        "schema": FAILURE_SCHEMA,
        "failure_id": failure_id,
        "kind": kind,
        "component": component,
        "source": source,
        "invariant": invariant,
        "failure_kind": failure_kind,
        "detail": detail,
    }
    path = output_dir / "failures" / f"{hashlib.sha256(failure_id.encode()).hexdigest()[:20]}.json"
    if path.exists():
        existing = _read_json(path)
        if existing != payload:
            raise ContractError(f"conflicting duplicate failure identity: {failure_id}")
        return existing
    _write_json(path, payload)
    return payload


def collect_junit_failures(
    *,
    output_dir: Path,
    lane: str,
    source: str,
    junit_path: Path,
    command_exit: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    parse_error: str | None = None
    try:
        root = ElementTree.parse(junit_path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        root = None
        parse_error = str(exc)
    if root is not None:
        for testcase in root.iter("testcase"):
            classname = testcase.attrib.get("classname", "").strip()
            name = testcase.attrib.get("name", "").strip() or "unnamed-test"
            invariant = f"{classname}::{name}" if classname else name
            for tag, failure_kind in (("failure", "test_failure"), ("error", "test_error")):
                problem = testcase.find(tag)
                if problem is None:
                    continue
                detail = (problem.attrib.get("message") or (problem.text or "")).strip() or None
                records.append(
                    record_failure(
                        output_dir=output_dir,
                        kind="lane",
                        component=lane,
                        source=source,
                        invariant=invariant,
                        failure_kind=failure_kind,
                        detail=detail,
                    )
                )
    if command_exit != 0 and not records:
        detail = f"command exited with status {command_exit}"
        if parse_error:
            detail += f"; JUnit unavailable: {parse_error}"
        records.append(
            record_failure(
                output_dir=output_dir,
                kind="lane",
                component=lane,
                source=source,
                invariant=f"{source} command",
                failure_kind="command_failed",
                detail=detail,
            )
        )
    return records


def collect_native_report_failures(
    *,
    output_dir: Path,
    lane: str,
    source: str,
    report_path: Path,
    command_exit: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        report = _read_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        report = None
        parse_error = str(exc)
    else:
        parse_error = None
    if isinstance(report, Mapping):
        tests = report.get("tests") if isinstance(report.get("tests"), Mapping) else {}
        for key, failure_kind in (("failed_nodeids", "test_failure"), ("error_nodeids", "test_error")):
            for nodeid in tests.get(key, []) or []:
                records.append(
                    record_failure(
                        output_dir=output_dir, kind="lane", component=lane, source=source,
                        invariant=str(nodeid), failure_kind=failure_kind,
                    )
                )
        environment_error = str(report.get("environment_error", "")).strip()
        if environment_error:
            records.append(
                record_failure(
                    output_dir=output_dir, kind="lane", component=lane, source=source,
                    invariant="native PostgreSQL environment availability",
                    failure_kind="environment_unavailable", detail=environment_error,
                )
            )
        for key, failure_kind in (("inventory_missing", "inventory_missing"), ("inventory_unexpected", "inventory_unexpected"), ("unwaived_skips", "unwaived_skip")):
            for value in report.get(key, []) or []:
                records.append(
                    record_failure(
                        output_dir=output_dir, kind="lane", component=lane, source=source,
                        invariant=f"{key}:{value}", failure_kind=failure_kind,
                    )
                )
        if report.get("identity_matches_fixture") is False:
            records.append(
                record_failure(
                    output_dir=output_dir, kind="lane", component=lane, source=source,
                    invariant="native PostgreSQL identity matches pytest fixture",
                    failure_kind="identity_mismatch",
                )
            )
    if command_exit != 0 and not records:
        detail = f"command exited with status {command_exit}"
        if parse_error:
            detail += f"; native report unavailable: {parse_error}"
        records.append(
            record_failure(
                output_dir=output_dir, kind="lane", component=lane, source=source,
                invariant=f"{source} command", failure_kind="command_failed", detail=detail,
            )
        )
    return records


def _load_failures(output_dir: Path) -> list[dict[str, Any]]:
    failures_dir = output_dir / "failures"
    if not failures_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(failures_dir.glob("*.json")):
        payload = _read_json(path)
        if payload.get("schema") != FAILURE_SCHEMA:
            raise ContractError(f"unexpected failure schema in {path}")
        failure_id = str(payload.get("failure_id", ""))
        if failure_id in seen:
            raise ContractError(f"duplicate failure identity in evidence inputs: {failure_id}")
        seen.add(failure_id)
        records.append(payload)
    return records


def record_component(
    *,
    output_dir: Path,
    kind: str,
    name: str,
    status: str,
    exit_code: int | None,
    duration_seconds: float,
    started_at: str,
    finished_at: str,
    command: Sequence[str] | None = None,
    failure_kind: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    if kind not in {"lane", "phase"}:
        raise ContractError(f"unsupported component kind: {kind}")
    if status not in {"passed", "failed"}:
        raise ContractError(f"unsupported component status: {status}")
    payload: dict[str, Any] = {
        "schema": COMPONENT_SCHEMA,
        "kind": kind,
        "name": name,
        "status": status,
        "exit_code": exit_code,
        "duration_seconds": round(max(duration_seconds, 0.0), 3),
        "started_at": started_at,
        "finished_at": finished_at,
        "failure_kind": failure_kind,
        "detail": detail,
        "command": list(command) if command else None,
    }
    _write_json(_component_path(output_dir, kind, name), payload)
    return payload


def run_component(
    *,
    output_dir: Path,
    kind: str,
    name: str,
    command: Sequence[str],
) -> dict[str, Any]:
    if not command:
        raise ContractError("component command must not be empty")
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-")
    log_path = logs_dir / f"{kind}-{safe_name}.log"
    started_at = _utc_now()
    started = time.monotonic()
    exit_code: int | None = None
    failure_kind: str | None = None
    detail: str | None = None

    print(f"BEGIN {kind} [{name}]", flush=True)
    print("COMMAND " + " ".join(command), flush=True)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                log.write(line)
            exit_code = process.wait()
        if exit_code != 0:
            failure_kind = "command_failed"
            detail = f"command exited with status {exit_code}"
    except OSError as exc:
        failure_kind = "runner_unavailable"
        detail = str(exc)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"runner unavailable: {exc}\n")
        print(f"UNAVAILABLE {kind} [{name}]: {exc}", file=sys.stderr, flush=True)

    duration = time.monotonic() - started
    finished_at = _utc_now()
    status = "passed" if exit_code == 0 and failure_kind is None else "failed"
    payload = record_component(
        output_dir=output_dir,
        kind=kind,
        name=name,
        status=status,
        exit_code=exit_code,
        duration_seconds=duration,
        started_at=started_at,
        finished_at=finished_at,
        command=command,
        failure_kind=failure_kind,
        detail=detail,
    )
    print(
        f"{status.upper()} {kind} [{name}] duration_seconds={duration:.2f}",
        flush=True,
    )
    return payload


def record_action_phase(
    *, output_dir: Path, name: str, outcome: str, detail: str | None = None
) -> dict[str, Any]:
    status = "passed" if outcome == "success" else "failed"
    now = _utc_now()
    return record_component(
        output_dir=output_dir,
        kind="phase",
        name=name,
        status=status,
        exit_code=0 if status == "passed" else None,
        duration_seconds=0.0,
        started_at=now,
        finished_at=now,
        failure_kind=None if status == "passed" else "action_failed",
        detail=detail or (None if status == "passed" else f"GitHub Actions outcome: {outcome}"),
    )


def _load_components(output_dir: Path) -> list[dict[str, Any]]:
    components_dir = output_dir / "components"
    if not components_dir.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(components_dir.glob("*.json")):
        payload = _read_json(path)
        if payload.get("schema") != COMPONENT_SCHEMA:
            raise ContractError(f"unexpected component schema in {path}")
        result.append(payload)
    return result


def finalize_run(*, output_dir: Path, evidence_path: Path) -> dict[str, Any]:
    state = _read_json(output_dir / "run-state.json")
    if state.get("schema") != RUN_STATE_SCHEMA:
        raise ContractError("run-state schema mismatch")

    components = _load_components(output_dir)
    detailed_failures = _load_failures(output_dir)
    by_lane = {
        component["name"]: component
        for component in components
        if component.get("kind") == "lane"
    }
    phases = [component for component in components if component.get("kind") == "phase"]
    failures: list[dict[str, Any]] = []
    lane_results: dict[str, Any] = {}

    detailed_by_lane: dict[str, list[dict[str, Any]]] = {lane: [] for lane in LANES}
    for failure in detailed_failures:
        if failure.get("kind") != "lane" or failure.get("component") not in LANES:
            raise ContractError(
                f"detailed failure must belong to a governed lane: {failure.get('failure_id')}"
            )
        detailed_by_lane[str(failure["component"])].append(failure)

    for lane in LANES:
        component = by_lane.get(lane)
        lane_failures = detailed_by_lane[lane]
        if component is None:
            failure = record_failure(
                output_dir=output_dir, kind="lane", component=lane, source="lane-result",
                invariant="required lane result exists", failure_kind="missing_result",
                detail="required lane produced no result",
            )
            lane_failures = [failure]
            lane_results[lane] = {
                "status": "failed", "duration_seconds": 0.0, "exit_code": None,
                "failure_ids": [failure["failure_id"]], "failure_kind": "missing_result",
            }
            failures.extend(lane_failures)
            continue
        if component["status"] == "failed" and not lane_failures:
            lane_failures = [
                record_failure(
                    output_dir=output_dir, kind="lane", component=lane, source="lane-command",
                    invariant="lane command",
                    failure_kind=component.get("failure_kind") or "command_failed",
                    detail=component.get("detail"),
                )
            ]
        if component["status"] == "passed" and lane_failures:
            raise ContractError(f"passed lane {lane} cannot contain detailed failures")
        lane_results[lane] = {
            "status": component["status"],
            "duration_seconds": component["duration_seconds"],
            "exit_code": component["exit_code"],
            "failure_ids": [failure["failure_id"] for failure in lane_failures],
            "failure_kind": component.get("failure_kind"),
        }
        failures.extend(lane_failures)

    phase_results: dict[str, Any] = {}
    for component in phases:
        phase_failures: list[dict[str, Any]] = []
        if component["status"] == "failed":
            phase_failures = [
                record_failure(
                    output_dir=output_dir, kind="phase", component=component["name"],
                    source="phase", invariant=f"setup phase {component['name']}",
                    failure_kind=component.get("failure_kind") or "phase_failed",
                    detail=component.get("detail"),
                )
            ]
            failures.extend(phase_failures)
        phase_results[component["name"]] = {
            "status": component["status"],
            "duration_seconds": component["duration_seconds"],
            "exit_code": component["exit_code"],
            "failure_ids": [failure["failure_id"] for failure in phase_failures],
            "failure_kind": component.get("failure_kind"),
        }

    completed_epoch = time.time()
    started_epoch = float(state["started_at_epoch"])
    total_duration = max(0.0, completed_epoch - started_epoch)
    estimated_billed_minutes = max(1, math.ceil(total_duration / 60.0))
    overall_result = "failed" if failures else "passed"
    failures = sorted(failures, key=lambda item: item["failure_id"])
    payload = {
        "schema": EVIDENCE_SCHEMA,
        "workflow": "Full regression",
        "main_sha": state["main_sha"],
        "previous_main_sha": state.get("previous_main_sha"),
        "commit_range": state.get("commit_range"),
        "previous_run_id": state.get("previous_run_id"),
        "run_id": state["run_id"],
        "run_attempt": state["run_attempt"],
        "event": state["event"],
        "manual_reason": state.get("manual_reason"),
        "workflow_ref": state["workflow_ref"],
        "started_at": state["started_at"],
        "completed_at": datetime.fromtimestamp(completed_epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "total_duration_seconds": round(total_duration, 3),
        "estimated_billed_minutes": estimated_billed_minutes,
        "overall_result": overall_result,
        "lane_results": lane_results,
        "phase_results": phase_results,
        "failures": failures,
        "triage": {
            "classification_schema": TRIAGE_SCHEMA,
            "required_failure_ids": [failure["failure_id"] for failure in failures],
            "complete": not failures,
        },
        "runner_integration": {
            "contract": "command-adapter-v1",
            "note": (
                "full regression owns evidence/continuation semantics; concrete lane commands may "
                "be replaced by the shared certification runner while preserving distinct failure records"
            ),
        },
    }
    _write_json(evidence_path, payload)
    return payload


def validate_triage_record(
    record: Mapping[str, Any], evidence: Mapping[str, Any] | None = None
) -> None:
    if record.get("schema") != TRIAGE_SCHEMA:
        raise ContractError(f"triage schema must be {TRIAGE_SCHEMA}")
    classification = record.get("classification")
    if classification not in CLASSIFICATIONS:
        raise ContractError(f"invalid classification: {classification!r}")
    main_sha = _require_sha(str(record.get("main_sha", "")), "triage main_sha")
    failure_id = str(record.get("failure_id", "")).strip()
    if not failure_id:
        raise ContractError("triage failure_id is required")
    if not str(record.get("analysis", "")).strip():
        raise ContractError("triage analysis is required")
    if not str(record.get("full_regression_run_id", "")).strip():
        raise ContractError("full_regression_run_id is required")

    if evidence is not None:
        if evidence.get("schema") != EVIDENCE_SCHEMA:
            raise ContractError("evidence schema mismatch")
        if main_sha != evidence.get("main_sha"):
            raise ContractError("triage main_sha does not match evidence")
        if str(record["full_regression_run_id"]) != str(evidence.get("run_id")):
            raise ContractError("triage run ID does not match evidence")
        required = set(evidence.get("triage", {}).get("required_failure_ids", []))
        if failure_id not in required:
            raise ContractError(f"triage failure_id is not required by evidence: {failure_id}")
        evidence_failure = next(
            (item for item in evidence.get("failures", []) if item.get("failure_id") == failure_id),
            None,
        )
        if not isinstance(evidence_failure, Mapping):
            raise ContractError(f"evidence missing failure record for required ID: {failure_id}")
    else:
        evidence_failure = None

    if classification != "related regression":
        if record.get("selector_miss") is True:
            raise ContractError("only a related regression can be a selector miss")
        return

    responsible = record.get("responsible_change")
    if not isinstance(responsible, Mapping):
        raise ContractError("related regression requires responsible_change")
    pr_number = responsible.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ContractError("responsible_change.pr_number must be a positive integer")
    certification = record.get("certification")
    if not isinstance(certification, Mapping):
        raise ContractError("related regression requires certification plan/run identity")
    for key in ("plan_id", "run_id"):
        if not str(certification.get(key, "")).strip():
            raise ContractError(f"certification.{key} is required")
    certification_sha = _require_sha(
        str(certification.get("candidate_sha", "")), "certification.candidate_sha"
    )
    responsible_sha = _require_sha(
        str(responsible.get("head_sha", "")), "responsible_change.head_sha"
    )
    if certification_sha != responsible_sha:
        raise ContractError("certification.candidate_sha must equal responsible_change.head_sha")

    failing_invariant = str(record.get("failing_invariant", "")).strip()
    if not failing_invariant:
        raise ContractError("related regression requires failing_invariant")
    failing_lane = str(record.get("failing_lane", "")).strip()
    if not failing_lane:
        raise ContractError("related regression requires failing_lane")
    if evidence_failure is not None:
        if evidence_failure.get("kind") != "lane":
            raise ContractError("related regression must reference a lane failure")
        if failing_lane != str(evidence_failure.get("component", "")):
            raise ContractError("triage failing_lane does not match evidence failure component")
        if failing_invariant != str(evidence_failure.get("invariant", "")):
            raise ContractError("triage failing_invariant does not match evidence failure invariant")
    if not isinstance(record.get("selector_miss"), bool):
        raise ContractError("related regression requires boolean selector_miss")

    if record.get("selector_miss") is not True:
        return

    correction = record.get("required_selector_correction")
    if not isinstance(correction, Mapping):
        raise ContractError("selector miss requires required_selector_correction")
    policy_paths = correction.get("policy_update_paths")
    if not isinstance(policy_paths, list) or not policy_paths or not all(
        isinstance(item, str) and item.strip() for item in policy_paths
    ):
        raise ContractError("selector miss requires non-empty policy_update_paths")
    regression = correction.get("representative_selector_regression")
    if not isinstance(regression, Mapping):
        raise ContractError("selector miss requires representative_selector_regression")
    for key in ("test_path", "changed_path_class", "expected_lane"):
        if not str(regression.get(key, "")).strip():
            raise ContractError(
                f"representative_selector_regression.{key} is required for selector miss"
            )
    if str(regression.get("expected_lane", "")).strip() != str(record.get("failing_lane", "")).strip():
        raise ContractError("selector miss expected_lane must equal failing_lane")
    if not str(correction.get("owner_action", "")).strip():
        raise ContractError("selector miss requires required_selector_correction.owner_action")


def check_triage_coverage(*, evidence: Mapping[str, Any], triage_dir: Path) -> dict[str, Any]:
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        raise ContractError("evidence schema mismatch")
    required = list(evidence.get("triage", {}).get("required_failure_ids", []))
    records: dict[str, Path] = {}
    errors: list[str] = []
    if triage_dir.exists():
        for path in sorted(triage_dir.glob("*.json")):
            try:
                record = _read_json(path)
                validate_triage_record(record, evidence)
            except (ContractError, json.JSONDecodeError) as exc:
                errors.append(f"{path}: {exc}")
                continue
            failure_id = record["failure_id"]
            if failure_id in records:
                errors.append(
                    f"duplicate triage for {failure_id}: {records[failure_id]} and {path}"
                )
            else:
                records[failure_id] = path
    missing = [failure_id for failure_id in required if failure_id not in records]
    unexpected = sorted(set(records) - set(required))
    if unexpected:
        errors.append("unexpected triage failure IDs: " + ", ".join(unexpected))
    return {
        "complete": not missing and not errors,
        "required_failure_ids": required,
        "classified_failure_ids": sorted(records),
        "missing_failure_ids": missing,
        "errors": errors,
    }


def verify_selector_correction(
    *, triage: Mapping[str, Any], changed_paths: Iterable[str]
) -> dict[str, Any]:
    validate_triage_record(triage)
    if triage.get("classification") != "related regression" or triage.get("selector_miss") is not True:
        raise ContractError("selector correction verification requires a SELECTOR MISS triage record")
    changed = {str(path).strip().lstrip("./") for path in changed_paths if str(path).strip()}
    correction = triage["required_selector_correction"]
    policy_paths = {str(path).strip().lstrip("./") for path in correction["policy_update_paths"]}
    regression_path = str(correction["representative_selector_regression"]["test_path"]).strip().lstrip(
        "./"
    )
    matched_policy = sorted(changed & policy_paths)
    if not matched_policy:
        raise ContractError(
            "selector-miss correction must change at least one required selector policy path"
        )
    if regression_path not in changed:
        raise ContractError(
            "selector-miss correction must include the representative selector regression test"
        )
    return {
        "valid": True,
        "matched_policy_paths": matched_policy,
        "representative_selector_regression": regression_path,
        "expected_lane": correction["representative_selector_regression"]["expected_lane"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="full_regression")
    sub = parser.add_subparsers(dest="command", required=True)

    decide = sub.add_parser("decide", help="dedupe scheduled unchanged-main full runs")
    decide.add_argument("--runs-json", type=Path, required=True)
    decide.add_argument("--main-sha", required=True)
    decide.add_argument("--event", required=True)
    decide.add_argument("--current-run-id", required=True)
    decide.add_argument("--github-output", type=Path)

    begin = sub.add_parser("begin", help="record immutable full-run identity")
    begin.add_argument("--output-dir", type=Path, required=True)
    begin.add_argument("--main-sha", required=True)
    begin.add_argument("--previous-main-sha")
    begin.add_argument("--previous-run-id")
    begin.add_argument("--run-id", required=True)
    begin.add_argument("--run-attempt", required=True)
    begin.add_argument("--event", required=True)
    begin.add_argument("--reason", default="")
    begin.add_argument("--workflow-ref", required=True)
    begin.add_argument("--started-at-epoch", type=float)

    for name, kind in (("run-lane", "lane"), ("run-phase", "phase")):
        run = sub.add_parser(name, help=f"run and record one {kind} without failing fast")
        run.add_argument(f"--{kind}", required=True)
        run.add_argument("--output-dir", type=Path, required=True)
        run.add_argument("command_argv", nargs=argparse.REMAINDER)

    failure = sub.add_parser("record-failure", help="record one independently triageable lane failure")
    failure.add_argument("--output-dir", type=Path, required=True)
    failure.add_argument("--lane", choices=LANES, required=True)
    failure.add_argument("--source", required=True)
    failure.add_argument("--invariant", required=True)
    failure.add_argument("--failure-kind", required=True)
    failure.add_argument("--detail")

    junit = sub.add_parser("collect-junit", help="record every failed/error testcase from JUnit")
    junit.add_argument("--output-dir", type=Path, required=True)
    junit.add_argument("--lane", choices=LANES, required=True)
    junit.add_argument("--source", required=True)
    junit.add_argument("--junit", type=Path, required=True)
    junit.add_argument("--command-exit", type=int, required=True)

    native = sub.add_parser("collect-native-report", help="record native PostgreSQL report failures")
    native.add_argument("--output-dir", type=Path, required=True)
    native.add_argument("--lane", choices=LANES, required=True)
    native.add_argument("--source", required=True)
    native.add_argument("--report", type=Path, required=True)
    native.add_argument("--command-exit", type=int, required=True)

    record = sub.add_parser("record-phase", help="record a GitHub Action setup outcome")
    record.add_argument("--phase", required=True)
    record.add_argument("--outcome", required=True)
    record.add_argument("--detail")
    record.add_argument("--output-dir", type=Path, required=True)

    finalize = sub.add_parser("finalize", help="write terminal full-regression evidence")
    finalize.add_argument("--output-dir", type=Path, required=True)
    finalize.add_argument("--evidence", type=Path, required=True)

    enforce = sub.add_parser("enforce", help="fail the workflow after evidence upload when needed")
    enforce.add_argument("--evidence", type=Path, required=True)

    validate = sub.add_parser("validate-triage", help="validate one durable classification record")
    validate.add_argument("--evidence", type=Path, required=True)
    validate.add_argument("--triage", type=Path, required=True)

    coverage = sub.add_parser("check-triage", help="require one valid triage record per failure")
    coverage.add_argument("--evidence", type=Path, required=True)
    coverage.add_argument("--triage-dir", type=Path, required=True)

    correction = sub.add_parser(
        "verify-selector-correction",
        help="require policy + representative regression changes for a SELECTOR MISS",
    )
    correction.add_argument("--triage", type=Path, required=True)
    correction.add_argument("--changed-path", action="append", default=[], required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "decide":
            payload = decide_run(
                runs_payload=_read_json(args.runs_json),
                main_sha=args.main_sha,
                event=args.event,
                current_run_id=args.current_run_id,
            )
            _github_output(
                args.github_output,
                {
                    "should_run": payload["should_run"],
                    "previous_main_sha": payload["previous_main_sha"],
                    "previous_run_id": payload["previous_run_id"],
                    "previous_run_conclusion": payload["previous_run_conclusion"],
                    "equivalent_success_run_id": payload["equivalent_success_run_id"],
                },
            )
            print(json.dumps(payload, sort_keys=True))
            return 0

        if args.command == "begin":
            payload = begin_run(
                output_dir=args.output_dir,
                main_sha=args.main_sha,
                previous_main_sha=args.previous_main_sha,
                previous_run_id=args.previous_run_id,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                event=args.event,
                reason=args.reason,
                workflow_ref=args.workflow_ref,
                started_at_epoch=args.started_at_epoch,
            )
            print(json.dumps(payload, sort_keys=True))
            return 0

        if args.command in {"run-lane", "run-phase"}:
            kind = "lane" if args.command == "run-lane" else "phase"
            name = getattr(args, kind)
            command = list(args.command_argv)
            if command and command[0] == "--":
                command = command[1:]
            payload = run_component(
                output_dir=args.output_dir,
                kind=kind,
                name=name,
                command=command,
            )
            print(json.dumps(payload, sort_keys=True))
            # Diagnostic full regression never fails fast.  Terminal enforcement happens after
            # every required group has had a chance to run and evidence has been uploaded.
            return 0

        if args.command == "record-failure":
            payload = record_failure(
                output_dir=args.output_dir, kind="lane", component=args.lane,
                source=args.source, invariant=args.invariant, failure_kind=args.failure_kind,
                detail=args.detail,
            )
            print(json.dumps(payload, sort_keys=True))
            return 0

        if args.command == "collect-junit":
            payload = collect_junit_failures(
                output_dir=args.output_dir, lane=args.lane, source=args.source,
                junit_path=args.junit, command_exit=args.command_exit,
            )
            print(json.dumps({"failures": payload}, sort_keys=True))
            return 0

        if args.command == "collect-native-report":
            payload = collect_native_report_failures(
                output_dir=args.output_dir, lane=args.lane, source=args.source,
                report_path=args.report, command_exit=args.command_exit,
            )
            print(json.dumps({"failures": payload}, sort_keys=True))
            return 0

        if args.command == "record-phase":
            payload = record_action_phase(
                output_dir=args.output_dir,
                name=args.phase,
                outcome=args.outcome,
                detail=args.detail,
            )
            print(json.dumps(payload, sort_keys=True))
            return 0

        if args.command == "finalize":
            payload = finalize_run(output_dir=args.output_dir, evidence_path=args.evidence)
            print(json.dumps(payload, sort_keys=True))
            return 0

        if args.command == "enforce":
            evidence = _read_json(args.evidence)
            if evidence.get("schema") != EVIDENCE_SCHEMA:
                raise ContractError("evidence schema mismatch")
            if evidence.get("overall_result") != "passed":
                print(
                    "full regression failed; durable evidence was written and requires triage",
                    file=sys.stderr,
                )
                return 1
            return 0

        if args.command == "validate-triage":
            evidence = _read_json(args.evidence)
            triage = _read_json(args.triage)
            validate_triage_record(triage, evidence)
            print(json.dumps({"valid": True, "failure_id": triage["failure_id"]}, sort_keys=True))
            return 0

        if args.command == "check-triage":
            evidence = _read_json(args.evidence)
            payload = check_triage_coverage(evidence=evidence, triage_dir=args.triage_dir)
            print(json.dumps(payload, sort_keys=True))
            return 0 if payload["complete"] else 2

        if args.command == "verify-selector-correction":
            triage = _read_json(args.triage)
            payload = verify_selector_correction(triage=triage, changed_paths=args.changed_path)
            print(json.dumps(payload, sort_keys=True))
            return 0

    except (ContractError, json.JSONDecodeError, OSError) as exc:
        print(f"full_regression: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
