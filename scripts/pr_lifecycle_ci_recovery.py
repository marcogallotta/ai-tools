"""Typed required-CI recovery for the lifecycle controller."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import urllib.parse
from typing import Any, Mapping

from pr_lifecycle_support import LifecycleError, LifecycleState
from pr_lifecycle_helpers import parse_external_dependency
import pr_gate

INFRA_RETRY_MARKER = "dish-infrastructure-retry:v1"
BASELINE_OWNER_MARKER = "dish-current-main-corrective-owner:v1"
EXTERNAL_DEPENDENCY_MARKER = "dish-external-dependency:v1"
RETRY_BUDGET = 2
PROBE_SECONDS = 15 * 60


def _fields(body: str, marker: str) -> list[dict[str, str]]:
    values = []
    pattern = re.compile(rf"<!--\s*{re.escape(marker)}\s+(.*?)\s*-->", re.S | re.I)
    for match in pattern.finditer(body or ""):
        fields = {}
        for token in match.group(1).split():
            if "=" in token:
                key, value = token.split("=", 1)
                fields[key.lower()] = value
        values.append(fields)
    return values


def _check_identity(current: Any) -> str:
    return str((current.gate or {}).get("required_check") or pr_gate.REQUIRED_ORDINARY_CI_CONTEXT)


def _run_id(current: Any) -> int | None:
    for key in ("required_workflow_run_id", "workflow_run_id", "run_id"):
        try:
            value = int((current.gate or {}).get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _retry_records(comments: list[Mapping[str, Any]], *, head: str, run_id: int) -> list[dict[str, str]]:
    result = []
    for comment in comments:
        for fields in _fields(str(comment.get("body") or ""), INFRA_RETRY_MARKER):
            if fields.get("head", "").lower() == head.lower() and fields.get("run") == str(run_id):
                fields = dict(fields)
                fields["created_at"] = str(comment.get("created_at") or comment.get("updated_at") or "")
                result.append(fields)
    return result


def _retry_marker(head: str, run_id: int, attempt: int, action: str) -> str:
    return f"<!-- {INFRA_RETRY_MARKER} head={head} run={run_id} attempt={attempt} action={action} -->"


def handle_infrastructure(engine: Any, current: Any) -> Any:
    run_id = _run_id(current)
    if run_id is None:
        current.state = LifecycleState.WAITING_INFRASTRUCTURE
        current.residual_reason = "Infrastructure failure lacks an exact workflow-run identity; semantic mutation is forbidden."
        current.human_action = None
        return current
    comments = engine.github.get_comments(current.number)
    records = _retry_records(comments, head=current.head, run_id=run_id)
    retries = len([r for r in records if r.get("action") == "retry"])
    if retries < RETRY_BUDGET:
        engine.github.rerun_failed_workflow(run_id)
        marker = _retry_marker(current.head, run_id, retries + 1, "retry")
        engine.github.add_comment(
            current.number,
            marker + f"\nInfrastructure-only retry {retries + 1}/{RETRY_BUDGET} requested for unchanged exact head `{current.head}`. No source mutation authorized.\n\n— Dish PR lifecycle dispatcher",
        )
        return engine.inspect(engine.github.get_pr(current.number))

    newest_probe = None
    for record in records:
        if record.get("action") != "probe":
            continue
        try:
            ts = datetime.fromisoformat(record.get("created_at", "").replace("Z", "+00:00"))
        except ValueError:
            continue
        newest_probe = max(newest_probe, ts, key=lambda x: x.timestamp()) if newest_probe else ts
    now = engine.now()
    if newest_probe is None or (now - newest_probe).total_seconds() >= PROBE_SECONDS:
        # Same-run rerun is the capability probe; it never changes the candidate.
        engine.github.rerun_failed_workflow(run_id)
        engine.github.add_comment(
            current.number,
            _retry_marker(current.head, run_id, retries, "probe")
            + f"\nWAITING ON INFRASTRUCTURE capability probe requested for unchanged exact head `{current.head}`.\n\n— Dish PR lifecycle dispatcher",
        )
    current.state = LifecycleState.WAITING_INFRASTRUCTURE
    current.state_label = "WAITING ON INFRASTRUCTURE"
    current.residual_reason = "Required CI is infrastructure-owned after bounded unchanged-head retries; periodic same-head probes continue."
    current.human_action = None
    return current


def _owner_key(main_sha: str, check: str, evidence: str) -> str:
    return hashlib.sha256(f"{main_sha}\0{check}\0{evidence}".encode()).hexdigest()[:20]


def _marker_for_owner(main_sha: str, check: str, evidence: str) -> str:
    return (
        f"<!-- {BASELINE_OWNER_MARKER} key={_owner_key(main_sha, check, evidence)} main={main_sha} "
        f"check={urllib.parse.quote(check, safe='')} evidence={urllib.parse.quote(evidence, safe='')} -->"
    )


def ensure_baseline_owner(engine: Any, current: Any) -> Any:
    if engine.asana is None:
        current.residual_reason = "Current-main CI defect is proven, but Asana is unavailable to create/reuse the corrective owner."
        current.human_action = None
        return current
    gate = current.gate or {}
    main_sha = str(gate.get("failure_main_sha") or gate.get("current_main_sha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", main_sha):
        # The ownership evidence may encode the main SHA; require an exact identity instead of guessing current main.
        evidence = str(gate.get("failure_ownership_evidence") or "")
        match = re.search(r"\b[0-9a-f]{40}\b", evidence.lower())
        if match:
            main_sha = match.group(0)
    if not re.fullmatch(r"[0-9a-f]{40}", main_sha):
        current.residual_reason = "Current-main ownership lacks an exact main SHA; corrective owner creation fails closed."
        return current
    check = _check_identity(current)
    evidence = str(gate.get("failure_ownership_evidence") or "proven current-main failure")
    marker = _marker_for_owner(main_sha, check, evidence)

    project_ids: list[str] = []
    for task in current.asana:
        for membership in task.get("memberships") or []:
            if isinstance(membership, Mapping) and isinstance(membership.get("project"), Mapping):
                gid = str(membership["project"].get("gid") or "")
                if gid and gid not in project_ids:
                    project_ids.append(gid)
    if not project_ids:
        current.residual_reason = "Current-main failure owner cannot be placed because no owning task project is available."
        return current
    project_gid = project_ids[0]
    owner = engine.asana.find_task_by_marker(project_gid, BASELINE_OWNER_MARKER, marker)
    if owner is None:
        owner = engine.asana.create_task(
            project_gid,
            name=f"P0 — CURRENT MAIN CORRECTIVE OWNER — {check}",
            notes=(
                f"OWNER: Implementation\nSTATE: BASELINE CORRECTIVE OWNER\nMAIN SHA: {main_sha}\nCHECK: {check}\n\n"
                f"{marker}\nEvidence: {evidence}\nThis task deduplicates all blocked candidates for this exact baseline defect."
            ),
        )
    owner_gid = str(owner.get("gid") or "")
    if not owner_gid:
        raise LifecycleError("baseline corrective owner creation/readback lacks task GID")

    encoded_check = urllib.parse.quote(check, safe="")
    encoded_evidence = urllib.parse.quote(evidence, safe="")
    dependency_marker = (
        f"<!-- {EXTERNAL_DEPENDENCY_MARKER} action=blocked task={owner_gid} check={encoded_check} "
        f"main={main_sha} evidence={encoded_evidence} reason={urllib.parse.quote('proven current-main failure', safe='')} -->"
    )
    existing = parse_external_dependency(engine.github.get_comments(current.number))
    if existing is None or existing.task_gid != owner_gid or existing.main_sha != main_sha:
        engine.github.add_comment(
            current.number,
            dependency_marker
            + f"\nCandidate is blocked on shared current-main corrective task `{owner_gid}`. The candidate head remains unchanged.\n\n— Dish PR lifecycle dispatcher",
        )
    return engine.inspect(engine.github.get_pr(current.number))


def recover_failed_ci(engine: Any, current: Any) -> Any:
    gate = current.gate or {}
    if gate.get("diagnosis") != pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value:
        return current
    classification = str(gate.get("failure_ownership") or "AMBIGUOUS")
    if classification == "INFRASTRUCTURE":
        return handle_infrastructure(engine, current)
    if classification == "PROVEN_CURRENT_MAIN":
        return ensure_baseline_owner(engine, current)
    return current
