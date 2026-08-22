"""Typed required-CI recovery for the lifecycle controller."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import urllib.parse
from typing import Any, Mapping

from pr_lifecycle_support import LifecycleError, LifecycleState
from pr_lifecycle_helpers import parse_external_dependency
from pr_lifecycle_task_state import apply_transition, task_snapshot
import pr_gate
from ci_failure_fingerprint import CausalIdentity, FingerprintError, validate_cause

INFRA_RETRY_MARKER = "dish-infrastructure-retry:v1"
BASELINE_OWNER_MARKER = "dish-current-main-corrective-owner:v1"
BASELINE_OCCURRENCE_MARKER = "dish-current-main-corrective-occurrence:v1"
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


def _marker_for_owner(fingerprint: str) -> str:
    key = fingerprint.split(":", 1)[1][:20]
    return f"<!-- {BASELINE_OWNER_MARKER} key={key} fingerprint={fingerprint} -->"


@dataclass(frozen=True)
class CorrectiveOccurrence:
    source: str
    classification: str
    main_sha: str
    check: str
    evidence: str
    run_id: str
    pr_number: int | None = None
    head_sha: str | None = None


def _occurrence_marker(*, fingerprint: str, occurrence: CorrectiveOccurrence) -> str:
    return (
        f"<!-- {BASELINE_OCCURRENCE_MARKER} fingerprint={fingerprint} "
        f"source={urllib.parse.quote(occurrence.source, safe='')} "
        f"pr={occurrence.pr_number or 'none'} head={occurrence.head_sha or 'none'} "
        f"main={occurrence.main_sha} run={urllib.parse.quote(occurrence.run_id, safe='')} "
        f"check={urllib.parse.quote(occurrence.check, safe='')} -->"
    )


def _ready_section(asana: Any, project_gid: str) -> str:
    sections = asana.get_project_sections(project_gid)
    matches = [str(item.get("gid") or "") for item in sections if item.get("name") == "Ready"]
    if len(matches) != 1 or not matches[0]:
        raise LifecycleError("corrective owner project must have exactly one Ready section")
    return matches[0]


def ensure_corrective_owner(
    asana: Any,
    *,
    project_gid: str,
    cause: CausalIdentity,
    occurrence: CorrectiveOccurrence,
) -> dict[str, Any]:
    marker = _marker_for_owner(cause.fingerprint)
    owner = asana.find_task_by_marker(project_gid, BASELINE_OWNER_MARKER, marker)
    if owner is None:
        owner = asana.create_task(
            project_gid,
            name=f"P0 — CURRENT MAIN CORRECTIVE OWNER — {occurrence.check}",
            notes=(
                f"OWNER: Implementation\nSTATE: BASELINE CORRECTIVE OWNER\n"
                f"FIRST AFFECTED SHA: {occurrence.main_sha}\nCHECK: {occurrence.check}\n\n"
                f"CAUSAL FINGERPRINT: {cause.fingerprint}\n{marker}\n"
                f"CAUSAL IDENTITY: {cause.json()}\nFirst evidence: {occurrence.evidence}\n"
                "This task deduplicates every occurrence of the same normalized defect; run and "
                "commit identities remain attached occurrence evidence."
            ),
        )
    owner_gid = str(owner.get("gid") or "")
    if not owner_gid:
        raise LifecycleError("baseline corrective owner creation/readback lacks task GID")

    ready_section = _ready_section(asana, project_gid)
    owner = asana.get_task(owner_gid)
    snapshot = task_snapshot(owner)
    in_ready = any(
        membership["project"] == project_gid and membership["section"] == ready_section
        for membership in snapshot["memberships"]
    )
    if owner.get("completed") is True or not in_ready:
        apply_transition(
            asana,
            owner_gid,
            expected=snapshot,
            desired={"completed": False, "section": ready_section},
            kind="baseline-corrective-owner-actionable",
        )
    owner = asana.get_task(owner_gid)
    verified = task_snapshot(owner)
    if owner.get("completed") is True or not any(
        membership["project"] == project_gid and membership["section"] == ready_section
        for membership in verified["memberships"]
    ):
        raise LifecycleError("baseline corrective owner actionable-state readback failed")

    occurrence_marker = _occurrence_marker(
        fingerprint=cause.fingerprint, occurrence=occurrence
    )
    stories = asana.get_stories(owner_gid)
    if not any(occurrence_marker in str(story.get("text") or "") for story in stories):
        asana.add_comment(
            owner_gid,
            occurrence_marker
            + f"\nCURRENT AFFECTED SHA: {occurrence.main_sha}\n"
            + f"CLASSIFICATION: {occurrence.classification}\n"
            + f"EVIDENCE: {occurrence.evidence}\nREPAIR OWNER: Implementation",
        )
        if not any(
            occurrence_marker in str(story.get("text") or "")
            for story in asana.get_stories(owner_gid)
        ):
            raise LifecycleError("baseline corrective occurrence readback failed")
    return owner


def ensure_scheduled_baseline_owner(
    asana: Any, *, project_gid: str, evidence: Mapping[str, Any], triage: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Route one already-classified scheduled/manual baseline failure to the shared owner."""
    if evidence.get("schema") != "dish-full-regression-v1":
        raise LifecycleError("scheduled route requires full-regression v1 evidence")
    if triage.get("schema") != "dish-full-regression-triage-v1":
        raise LifecycleError("scheduled route requires full-regression triage v1")
    if str(triage.get("full_regression_run_id") or "") != str(evidence.get("run_id") or ""):
        raise LifecycleError("scheduled triage run does not match evidence")
    if str(triage.get("main_sha") or "").lower() != str(evidence.get("main_sha") or "").lower():
        raise LifecycleError("scheduled triage main SHA does not match evidence")
    if triage.get("classification") != "unrelated baseline":
        return None
    failure_id = str(triage.get("failure_id") or "")
    failure = next(
        (item for item in evidence.get("failures", []) if item.get("failure_id") == failure_id),
        None,
    )
    if not isinstance(failure, Mapping):
        raise LifecycleError("scheduled triage does not reference exact failure evidence")
    try:
        cause = validate_cause(
            fingerprint=str(triage.get("causal_fingerprint") or ""),
            identity=failure.get("causal_identity") if isinstance(failure.get("causal_identity"), Mapping) else {},
        )
    except FingerprintError as exc:
        raise LifecycleError(f"scheduled baseline causal identity is ambiguous: {exc}") from exc
    main_sha = str(evidence.get("main_sha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", main_sha):
        raise LifecycleError("scheduled baseline evidence lacks exact main SHA")
    event = str(evidence.get("event") or "")
    source = "scheduled" if event == "schedule" else "manual"
    occurrence = CorrectiveOccurrence(
        source=source,
        classification="UNRELATED_BASELINE",
        main_sha=main_sha,
        check=str(failure.get("component") or "full-regression"),
        evidence=str(triage.get("analysis") or "classified full-regression baseline failure"),
        run_id=str(evidence.get("run_id") or ""),
        head_sha=main_sha,
    )
    return ensure_corrective_owner(
        asana, project_gid=project_gid, cause=cause, occurrence=occurrence
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
    try:
        cause = validate_cause(
            fingerprint=str(gate.get("failure_causal_fingerprint") or ""),
            identity=gate.get("failure_causal_identity")
            if isinstance(gate.get("failure_causal_identity"), Mapping)
            else {},
        )
    except FingerprintError:
        current.residual_reason = (
            "Current-main ownership lacks a strong causal fingerprint; corrective owner creation "
            "fails closed as ambiguous."
        )
        return current

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
    occurrence = CorrectiveOccurrence(
        source="pr",
        classification="PROVEN_CURRENT_MAIN",
        main_sha=main_sha,
        check=check,
        evidence=evidence,
        run_id=str(_run_id(current) or "unknown"),
        pr_number=current.number,
        head_sha=current.head.lower(),
    )
    owner = ensure_corrective_owner(
        engine.asana, project_gid=project_gid, cause=cause, occurrence=occurrence
    )
    owner_gid = str(owner["gid"])

    encoded_check = urllib.parse.quote(check, safe="")
    encoded_evidence = urllib.parse.quote(evidence, safe="")
    dependency_marker = (
        f"<!-- {EXTERNAL_DEPENDENCY_MARKER} action=blocked task={owner_gid} check={encoded_check} "
        f"head={current.head.lower()} main={main_sha} fingerprint={cause.fingerprint} evidence={encoded_evidence} "
        f"reason={urllib.parse.quote('proven current-main failure', safe='')} -->"
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
