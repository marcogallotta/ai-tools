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
SOURCE_LANDING_HOLD_MARKER = "dish-source-landing-hold:v1"

_STATE_LINE_RE = re.compile(r"(?mi)^STATE:\s*(?P<state>[^\n]+)$")
_HOLD_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")
_DECISION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,120}$")


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


def structured_story(prefix: str, payload: Mapping[str, Any]) -> str:
    """Encode one exact append-only authority record.

    Exact whole-comment framing is intentional: prose that merely resembles a
    marker cannot become machine transition authority.
    """
    value = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"<!-- {prefix} {value} -->"


def structured_story_payload(story: Mapping[str, Any], prefix: str) -> dict[str, Any] | None:
    text = _story_text(story)
    start = f"<!-- {prefix} "
    if not text.startswith(start) or not text.endswith(" -->") or "\n" in text:
        return None
    raw = text[len(start):-4]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _story_time(story: Mapping[str, Any]) -> datetime | None:
    raw = story.get("created_at") or story.get("updated_at")
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _story_order(story_id: str) -> tuple[int, int | str]:
    if story_id.isdigit():
        return (0, int(story_id))
    return (1, story_id)


def source_landing_hold(stories: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Reconstruct the only Asana-side V3 source-landing veto.

    A valid hold/release is an exact whole-comment structured story.  Agent-authored
    discussion is required by standing policy to carry a Dish Agent footer, so it
    cannot accidentally satisfy this exact framing.  Authenticated account metadata
    is deliberately ignored: the explicit structured decision record is the
    provenance-bearing action, not the service account name.
    """
    events: list[tuple[datetime, tuple[int, int | str], str, dict[str, str]]] = []
    malformed: list[dict[str, Any]] = []

    for story in stories:
        text = _story_text(story)
        if SOURCE_LANDING_HOLD_MARKER not in text:
            continue
        payload = structured_story_payload(story, SOURCE_LANDING_HOLD_MARKER)
        when = _story_time(story)
        story_id = str(story.get("gid") or story.get("id") or "")
        if payload is None or when is None:
            malformed.append({
                "story": story_id or None,
                "reason": "hold marker must be one exact structured comment with a valid timestamp",
            })
            continue
        action = str(payload.get("action") or "").strip().lower()
        hold_id = str(payload.get("hold_id") or "").strip()
        decision = str(payload.get("decision") or "").strip()
        authority = str(payload.get("authority") or "").strip().lower()
        if (
            action not in {"hold", "release"}
            or _HOLD_ID_RE.fullmatch(hold_id) is None
            or _DECISION_ID_RE.fullmatch(decision) is None
            or authority not in {"marco", "authorized-human"}
        ):
            malformed.append({
                "story": story_id or None,
                "reason": "hold marker fields are invalid",
            })
            continue
        events.append((
            when,
            _story_order(story_id),
            story_id,
            {
                "action": action,
                "hold_id": hold_id,
                "decision": decision,
                "authority": authority,
            },
        ))

    if malformed:
        return {
            "state": "CONTRADICTION",
            "reason": "malformed explicit human hold evidence",
            "errors": malformed,
        }
    if not events:
        return {
            "state": "CLEAR",
            "reason": "no explicit durable human source-landing hold exists",
            "active_hold_id": None,
        }

    events.sort(key=lambda item: (item[0], item[1]))
    active: dict[str, str] | None = None
    last_release: dict[str, str] | None = None
    evidence_story = None
    evidence_time = None

    for when, _, story_id, event in events:
        evidence_story = story_id or None
        evidence_time = when.isoformat()
        if event["action"] == "hold":
            if active is None:
                active = event
                last_release = None
                continue
            if active == event:
                continue
            return {
                "state": "CONTRADICTION",
                "reason": "a second distinct hold appeared before explicit release",
                "active_hold_id": active["hold_id"],
                "evidence_story": evidence_story,
                "evidence_at": evidence_time,
            }

        # release
        if active is None:
            if last_release == event:
                continue
            return {
                "state": "CONTRADICTION",
                "reason": "release has no matching active hold",
                "evidence_story": evidence_story,
                "evidence_at": evidence_time,
            }
        if event["hold_id"] != active["hold_id"]:
            return {
                "state": "CONTRADICTION",
                "reason": "release does not match the active hold identity",
                "active_hold_id": active["hold_id"],
                "evidence_story": evidence_story,
                "evidence_at": evidence_time,
            }
        if event["decision"] == active["decision"]:
            return {
                "state": "CONTRADICTION",
                "reason": "release must carry a distinct explicit human decision identity",
                "active_hold_id": active["hold_id"],
                "evidence_story": evidence_story,
                "evidence_at": evidence_time,
            }
        last_release = event
        active = None

    if active is not None:
        return {
            "state": "HELD",
            "reason": "explicit durable human source-landing hold is active",
            "active_hold_id": active["hold_id"],
            "hold_decision": active["decision"],
            "authority": active["authority"],
            "evidence_story": evidence_story,
            "evidence_at": evidence_time,
        }
    return {
        "state": "CLEAR",
        "reason": "explicit human hold was released by a distinct durable decision",
        "active_hold_id": None,
        "release_decision": last_release["decision"] if last_release else None,
        "authority": last_release["authority"] if last_release else None,
        "evidence_story": evidence_story,
        "evidence_at": evidence_time,
    }


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
    story_values = list(stories)
    hold = source_landing_hold(story_values)
    evidence: list[tuple[datetime, str, str]] = []
    for story in story_values:
        text = _story_text(story)
        ts = _story_time(story)
        if ts is None:
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
        return {
            "state": "NO DURABLE EXECUTION EVIDENCE",
            "stale": False,
            "stale_kind": None,
            "timestamp": None,
            "source_landing_hold": hold,
        }
    ts, state, _ = max(evidence, key=lambda row: row[0])
    age = (now - ts).total_seconds()
    stale = False
    stale_kind = None
    if state == "DISPATCH REQUESTED" and age > 3600:
        state = "DISPATCH STALE — ACCEPTANCE NOT PROVEN"
        stale = True
        stale_kind = "WORKER_ACCEPTANCE_STALE"
    elif state == "HANDOFF RECORDED" and age > 3600:
        state = "STALE / EXECUTION UNKNOWN"
        stale = True
        stale_kind = "WORKER_EXECUTION_STALE"
    return {
        "state": state,
        "stale": stale,
        "stale_kind": stale_kind,
        "timestamp": ts.isoformat(),
        "source_landing_hold": hold,
    }


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
