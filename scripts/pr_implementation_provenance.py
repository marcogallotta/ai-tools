"""Independent Implementation-host provenance verification for Review routing.

Pre-PR provenance is accepted only when the post-publication GitHub cache can be
reconstructed back to the repository control plane's durable, pre-launch Asana
handoff record. Post-PR provenance remains delegated to the #95 broker verifier.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
import re
import zipfile
from typing import Any, Iterable, Mapping

import pr_lifecycle_helpers_base as base
from pr_lifecycle_owner import task_ids_from_pr
from pr_lifecycle_support import AsanaREST, LifecycleError

IMPLEMENTATION_HOST_WITNESS_MARKER = "dish-implementation-host-witness:v1"
IMPLEMENTATION_ROUTE_RESULT_MARKER = "dish-implementation-route-result:v1"
IMPLEMENTATION_PROVENANCE_SCHEMA = "dish-implementation-prelaunch-proof-v2"
IMPLEMENTATION_PROVENANCE_WORKFLOW = ".github/workflows/pr-implementation-provenance.yml"
IMPLEMENTATION_PROVENANCE_FILENAME = "implementation-provenance.json"
HANDOFF_MARKER = "dish-implementation-handoff:v1"
HANDOFF_ROLE = "Implementation"
LAUNCHER_PREFIX = "asana-handoff:"

_HANDOFF_RE = re.compile(
    rf"<!--\s*{re.escape(HANDOFF_MARKER)}\s+handoff=(?P<id>[0-9a-f]{{16}})\s+"
    r"task=(?P<task>\d{16})\s+role=(?P<role>[A-Za-z-]+)\s+at=(?P<at>[^\s]+)\s*-->"
)
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HANDOFF_CLOCK_SKEW_SECONDS = 300


def _asana_from_environment() -> Any | None:
    token = os.environ.get("ASANA_ACCESS_TOKEN", "").strip()
    return AsanaREST(token) if token else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expected_source(*, repository: str, task_gid: str, branch: str, base_ref: str, base_sha: str) -> str:
    return (
        "dish-prelaunch:v1 "
        f"repository={repository} task={task_gid} assignment=implementation "
        f"host=chatgpt branch={branch} base_ref={base_ref} base_sha={base_sha} "
        "existing_pr=none"
    )


def _handoff_identity(*, task_gid: str, timestamp: datetime, source: str) -> str:
    raw = f"{task_gid}\0{HANDOFF_ROLE}\0{timestamp.astimezone(timezone.utc).isoformat()}\0{source}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _verified_handoff_story(
    story: Mapping[str, Any],
    *,
    task_gid: str,
    handoff_id: str,
    repository: str,
    branch: str,
    base_ref: str,
    base_sha: str,
    workflow_started_at: datetime,
) -> bool:
    text = str(story.get("text") or "")
    matches = list(_HANDOFF_RE.finditer(text))
    if len(matches) != 1:
        return False
    marker = matches[0]
    if marker.group("id") != handoff_id or marker.group("task") != task_gid or marker.group("role") != HANDOFF_ROLE:
        return False

    handoff_at = _parse_time(marker.group("at"))
    story_created_at = _parse_time(story.get("created_at"))
    if handoff_at is None or story_created_at is None:
        return False
    if story_created_at > workflow_started_at or handoff_at > workflow_started_at:
        return False
    if abs((story_created_at - handoff_at).total_seconds()) > _HANDOFF_CLOCK_SKEW_SECONDS:
        return False

    source = _expected_source(
        repository=repository, task_gid=task_gid, branch=branch, base_ref=base_ref, base_sha=base_sha
    )
    if _handoff_identity(task_gid=task_gid, timestamp=handoff_at, source=source) != handoff_id:
        return False

    required_lines = {
        "AUTHORIZED IMPLEMENTATION HANDOFF",
        f"Task: {task_gid}",
        f"Target role: {HANDOFF_ROLE}",
        f"Handoff time: {handoff_at.isoformat()}",
        f"Source: {source}",
        f"Branch: {branch}",
        f"Base: {base_sha}",
        "PR: not yet known",
        "Head: not yet known",
        "— Dish Agent: Development Workflow | repository control plane",
    }
    return required_lines <= set(text.splitlines())


def _verified_prelaunch_host(
    pr: Mapping[str, Any],
    fields: Mapping[str, str],
    *,
    current_head: str,
    github: Any | None,
    asana: Any | None,
) -> str | None:
    required = (
        "head", "host", "source", "launcher", "task", "handoff", "base_ref",
        "base_sha", "run", "attempt", "artifact", "digest",
    )
    if github is None or asana is None or any(not fields.get(name) for name in required):
        return None
    if (
        fields["head"] != current_head
        or fields["host"] != "chatgpt"
        or fields["source"] != "orchestration"
        or fields["launcher"] != f"{LAUNCHER_PREFIX}{fields['handoff']}"
        or re.fullmatch(r"\d{16}", fields["task"]) is None
        or re.fullmatch(r"[0-9a-f]{16}", fields["handoff"]) is None
        or _FULL_SHA_RE.fullmatch(fields["base_sha"]) is None
    ):
        return None
    if fields["task"] not in task_ids_from_pr(pr):
        return None
    if fields["base_ref"] != f"refs/heads/{base._pr_base(pr)}":
        return None

    try:
        run_id, attempt, artifact_id = (int(fields[name]) for name in ("run", "attempt", "artifact"))
        run = github.get_workflow_run_attempt(run_id, attempt)
        workflow_started_at = _parse_time(run.get("run_started_at") or run.get("created_at"))
        if (
            workflow_started_at is None
            or int(run.get("id", -1)) != run_id
            or int(run.get("run_attempt", -1)) != attempt
            or str(run.get("event") or "") != "repository_dispatch"
            or str(run.get("conclusion") or "") != "success"
            or str(run.get("path") or run.get("workflow_path") or "") != IMPLEMENTATION_PROVENANCE_WORKFLOW
        ):
            return None
        artifacts = [
            value for value in github.get_run_artifacts(run_id)
            if int(value.get("id", -1)) == artifact_id and not bool(value.get("expired"))
        ]
        if len(artifacts) != 1 or str(artifacts[0].get("digest") or "").lower() != fields["digest"].lower():
            return None
        archive = github.download_artifact(artifact_id)
        if f"sha256:{hashlib.sha256(archive).hexdigest()}" != fields["digest"].lower():
            return None
        with zipfile.ZipFile(io.BytesIO(archive), "r") as zf:
            names = [name for name in zf.namelist() if not name.endswith("/")]
            if names != [IMPLEMENTATION_PROVENANCE_FILENAME]:
                return None
            proof = json.loads(zf.read(IMPLEMENTATION_PROVENANCE_FILENAME))
        if proof != {
            "schema": IMPLEMENTATION_PROVENANCE_SCHEMA,
            "repository": github.repository,
            "pr_number": base._pr_number(pr),
            "task_gid": fields["task"],
            "branch": base._pr_branch(pr),
            "head": current_head,
            "base_ref": fields["base_ref"],
            "base_sha": fields["base_sha"],
            "host": "chatgpt",
            "launcher": fields["launcher"],
            "handoff_id": fields["handoff"],
            "run_id": run_id,
            "run_attempt": attempt,
        }:
            return None
        stories = asana.get_stories(fields["task"])
        matches = [
            story for story in stories
            if _verified_handoff_story(
                story,
                task_gid=fields["task"],
                handoff_id=fields["handoff"],
                repository=github.repository,
                branch=base._pr_branch(pr),
                base_ref=fields["base_ref"],
                base_sha=fields["base_sha"],
                workflow_started_at=workflow_started_at,
            )
        ]
        if len(matches) != 1:
            return None
    except (
        AttributeError, KeyError, TypeError, ValueError, zipfile.BadZipFile,
        json.JSONDecodeError, LifecycleError,
    ):
        return None
    return "chatgpt"


def implementation_host_witness(
    pr: Mapping[str, Any],
    comments: Iterable[Mapping[str, Any]],
    *,
    current_head: str,
    github: Any | None = None,
) -> str | None:
    """Return independently proven exact-head Implementation provenance or fail closed."""
    hosts: set[str] = set()
    asana = _asana_from_environment()
    route_verifier = getattr(base, "_verified_route_result_host", None)
    for comment in comments:
        body = str(comment.get("body") or "")
        for fields in base._marker_fields(body, IMPLEMENTATION_HOST_WITNESS_MARKER):
            if _verified_prelaunch_host(pr, fields, current_head=current_head, github=github, asana=asana) == "chatgpt":
                hosts.add("CHATGPT_IMPLEMENTATION")
        if callable(route_verifier):
            for fields in base._marker_fields(body, IMPLEMENTATION_ROUTE_RESULT_MARKER):
                if route_verifier(pr, comments, fields, current_head=current_head, github=github) == "chatgpt":
                    hosts.add("CHATGPT_IMPLEMENTATION")
    return next(iter(hosts)) if len(hosts) == 1 else None
