"""Deterministic Asana task transition admission and lifecycle projection.

Asana does not provide an atomic compare-and-swap mutation.  This module therefore
uses an exact precondition snapshot, a stable transition identity, a scoped write,
and authoritative readback.  Concurrent movement is detected and surfaced; it is
never described as atomic CAS.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from pr_lifecycle_support import LifecycleError

TRANSITION_MARKER = "dish-lifecycle-transition:v1"
PROJECTION_MARKER = "dish-lifecycle-projection:v1"
ROLLOUT_PLAN_PREFIX = "dish-rollout-plan:v1"
ROLLOUT_TRANSITION_PREFIX = "dish-rollout-transition:v1"

_STATE_LINE_RE = re.compile(r"(?mi)^STATE:\s*(?P<state>[^\n]+)$")


def _json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def task_snapshot(task: Mapping[str, Any]) -> dict[str, Any]:
    memberships = []
    for membership in task.get("memberships") or []:
        if not isinstance(membership, Mapping):
            continue
        project = membership.get("project") if isinstance(membership.get("project"), Mapping) else {}
        section = membership.get("section") if isinstance(membership.get("section"), Mapping) else {}
        memberships.append({
            "project": str(project.get("gid") or ""),
            "section": str(section.get("gid") or ""),
        })
    memberships.sort(key=lambda x: (x["project"], x["section"]))
    notes = str(task.get("notes") or "")
    state_match = _STATE_LINE_RE.search(notes)
    return {
        "gid": str(task.get("gid") or ""),
        "modified_at": str(task.get("modified_at") or ""),
        "name": str(task.get("name") or ""),
        "notes_sha256": hashlib.sha256(notes.encode()).hexdigest(),
        "completed": bool(task.get("completed")),
        "memberships": memberships,
        "state_line": state_match.group("state").strip() if state_match else None,
    }


def transition_id(task_gid: str, expected: Mapping[str, Any], desired: Mapping[str, Any], kind: str) -> str:
    payload = {"task": task_gid, "kind": kind, "expected": expected, "desired": desired}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def _story_text(story: Mapping[str, Any]) -> str:
    return str(story.get("text") or story.get("body") or "")


def transition_already_recorded(stories: Iterable[Mapping[str, Any]], stable_id: str) -> bool:
    token = f"<!-- {TRANSITION_MARKER} id={stable_id} "
    return any(token in _story_text(story) for story in stories)


def _validate_precondition(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for key, value in expected.items():
        if key not in actual:
            raise LifecycleError(f"task transition precondition field is unsupported: {key}")
        if actual[key] != value:
            raise LifecycleError(
                f"task transition precondition moved: {key} expected {value!r}, got {actual[key]!r}"
            )


def _dependency_gate(task: Mapping[str, Any], *, kind: str) -> None:
    # Dependencies gate only transitions that claim execution can advance.  A stale
    # projection repair or terminal writeback must not be blocked by an unrelated
    # dependency merely because the task has one.
    gated = {"implementation-admit", "dispatch-request", "integration-admit"}
    if kind not in gated:
        return
    deps = task.get("dependencies") or []
    incomplete = [d for d in deps if isinstance(d, Mapping) and not bool(d.get("completed"))]
    if incomplete:
        gids = ",".join(str(d.get("gid") or "?") for d in incomplete)
        raise LifecycleError(f"task transition {kind} is blocked by incomplete dependencies: {gids}")


@dataclass(frozen=True)
class TaskTransitionResult:
    transition_id: str
    changed: bool
    readback: dict[str, Any]


def apply_transition(
    asana: Any,
    task_gid: str,
    *,
    expected: Mapping[str, Any],
    desired: Mapping[str, Any],
    kind: str,
) -> TaskTransitionResult:
    before = asana.get_task(task_gid)
    stable_id = transition_id(task_gid, expected, desired, kind)
    stories = asana.get_stories(task_gid)
    # A completed stable transition is authoritative replay evidence.  Check it
    # before the expected modified_at precondition, because our own successful
    # write necessarily moved modified_at.
    if transition_already_recorded(stories, stable_id):
        return TaskTransitionResult(stable_id, False, before)
    actual = task_snapshot(before)
    _validate_precondition(actual, expected)
    _dependency_gate(before, kind=kind)

    allowed = {"name", "notes", "completed"}
    unknown = set(desired) - allowed - {"section"}
    if unknown:
        raise LifecycleError(f"unsupported task transition fields: {sorted(unknown)}")
    mutation = {key: desired[key] for key in allowed if key in desired}
    if mutation:
        asana.update_projection_fields(task_gid, mutation)
    section = desired.get("section")
    if section:
        asana.move_task_to_section(task_gid, str(section))

    after = asana.get_task(task_gid)
    after_snapshot = task_snapshot(after)
    for key, value in mutation.items():
        if key == "notes":
            if hashlib.sha256(str(after.get("notes") or "").encode()).hexdigest() != hashlib.sha256(str(value).encode()).hexdigest():
                raise LifecycleError("task transition notes readback mismatch")
        elif bool(after.get(key)) != bool(value) if key == "completed" else str(after.get(key) or "") != str(value):
            raise LifecycleError(f"task transition {key} readback mismatch")
    if section and not any(m["section"] == str(section) for m in after_snapshot["memberships"]):
        raise LifecycleError("task transition section readback mismatch")

    marker = (
        f"<!-- {TRANSITION_MARKER} id={stable_id} kind={kind} "
        f"before={_json_digest(expected)} after={_json_digest(desired)} -->"
    )
    asana.add_comment(task_gid, marker + "\nLifecycle transition accepted after exact pre-read and authoritative readback.")
    if not transition_already_recorded(asana.get_stories(task_gid), stable_id):
        raise LifecycleError("task transition durable marker readback failed")
    return TaskTransitionResult(stable_id, True, after)


def execution_truth(task: Mapping[str, Any], stories: Iterable[Mapping[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Project durable handoff/dispatch evidence without upgrading section state to execution truth."""
    now = now or datetime.now(timezone.utc)
    evidence: list[tuple[datetime, str, str]] = []
    for story in stories:
        text = _story_text(story)
        raw = story.get("created_at") or story.get("updated_at")
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        upper = text.upper()
        if "RUNNING-SOURCE" in upper or "RUNNING SOURCE" in upper or "SOURCE EVIDENCE" in upper:
            evidence.append((ts, "RUNNING-SOURCE", text))
        elif "DISPATCH ACCEPTED" in upper or "DESTINATION ACCEPTED" in upper or "DESTINATION BOUND" in upper:
            evidence.append((ts, "DISPATCH ACCEPTED / BOUND", text))
        elif "DISPATCH REQUESTED" in upper or "DISPATCH INVOKED" in upper:
            evidence.append((ts, "DISPATCH REQUESTED", text))
        elif "HANDOFF RECORDED" in upper or "HANDOFF PREPARED" in upper or "HANDOFF SENT" in upper:
            evidence.append((ts, "HANDOFF RECORDED", text))
    if not evidence:
        return {"state": "NO DURABLE EXECUTION EVIDENCE", "stale": False, "timestamp": None}
    ts, state, _ = max(evidence, key=lambda row: row[0])
    stale = state == "HANDOFF RECORDED" and (now - ts).total_seconds() > 3600
    if stale:
        state = "STALE / EXECUTION UNKNOWN"
    return {"state": state, "stale": stale, "timestamp": ts.isoformat()}


def projection_comment(task_gid: str, projection: Mapping[str, Any]) -> str:
    digest = _json_digest(projection)
    return f"<!-- {PROJECTION_MARKER} task={task_gid} digest={digest} -->\n" + json.dumps(projection, sort_keys=True)


def ensure_projection_comment(asana: Any, task_gid: str, projection: Mapping[str, Any]) -> bool:
    body = projection_comment(task_gid, projection)
    marker = body.splitlines()[0]
    if any(marker in _story_text(story) for story in asana.get_stories(task_gid)):
        return False
    asana.add_comment(task_gid, body)
    if not any(marker in _story_text(story) for story in asana.get_stories(task_gid)):
        raise LifecycleError("task lifecycle projection marker readback failed")
    return True
