"""Durable Asana handoff projection for pre-PR Implementation dispatch.

This module does not create implementation authority or a second ownership system.  It
projects an already-authorized handoff onto the owning Asana task, verifies readback,
and provides an idempotent staleness observation for the no-PR case.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Any, Iterable, Mapping, Protocol


HANDOFF_MARKER = "dish-implementation-handoff:v1"
STALE_MARKER = "dish-stale-handoff:v1"
STALE_AFTER = timedelta(hours=3)
_HANDOFF_RE = re.compile(
    rf"<!--\s*{re.escape(HANDOFF_MARKER)}\s+handoff=(?P<id>[0-9a-f]{{16}})\s+"
    r"task=(?P<task>\d{16})\s+role=(?P<role>[A-Za-z-]+)\s+at=(?P<at>[^\s]+)\s*-->"
)
_STALE_RE = re.compile(
    rf"<!--\s*{re.escape(STALE_MARKER)}\s+handoff=(?P<id>[0-9a-f]{{16}})\s*-->"
)


class HandoffError(RuntimeError):
    pass


class HandoffAsana(Protocol):
    def get_task(self, gid: str) -> dict[str, Any]: ...
    def get_stories(self, gid: str) -> list[dict[str, Any]]: ...
    def add_comment(self, gid: str, text: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HandoffRecord:
    handoff_id: str
    task_gid: str
    target_role: str
    timestamp: datetime
    source: str
    branch: str | None
    base: str | None
    pr: int | None
    head: str | None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _identity(*, task_gid: str, target_role: str, timestamp: datetime, source: str) -> str:
    raw = f"{task_gid}\0{target_role}\0{_utc(timestamp).isoformat()}\0{source}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _section_ids(task: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for membership in task.get("memberships") or []:
        if not isinstance(membership, Mapping):
            continue
        section = membership.get("section")
        if isinstance(section, Mapping) and section.get("gid"):
            ids.add(str(section["gid"]))
    return ids


def _story_texts(stories: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(story.get("text") or "") for story in stories]


def _has_handoff(stories: Iterable[Mapping[str, Any]], handoff_id: str) -> bool:
    return any(
        match and match.group("id") == handoff_id
        for text in _story_texts(stories)
        for match in [_HANDOFF_RE.search(text)]
    )


def _has_stale_notice(stories: Iterable[Mapping[str, Any]], handoff_id: str) -> bool:
    return any(
        match and match.group("id") == handoff_id
        for text in _story_texts(stories)
        for match in [_STALE_RE.search(text)]
    )


def move_task_to_section(asana: Any, gid: str, section_gid: str) -> None:
    """Use an existing adapter method or the established AsanaREST transport surface."""
    mover = getattr(asana, "move_task_to_section", None)
    if callable(mover):
        mover(gid, section_gid)
        return
    http = getattr(asana, "http", None)
    api_root = getattr(asana, "api_root", None)
    headers = getattr(asana, "headers", None)
    request = getattr(http, "request", None)
    if not api_root or not isinstance(headers, Mapping) or not callable(request):
        raise HandoffError("Asana adapter cannot move task to a section")
    request(
        "POST",
        f"{str(api_root).rstrip('/')}/sections/{section_gid}/addTask",
        headers=headers,
        body={"data": {"task": gid}},
    )


def record_implementation_handoff(
    *,
    asana: HandoffAsana,
    task_gid: str,
    ready_section_gid: str,
    in_progress_section_gid: str,
    target_role: str,
    timestamp: datetime,
    source: str,
    branch: str | None = None,
    base: str | None = None,
    pr: int | None = None,
    head: str | None = None,
) -> HandoffRecord:
    """Project an already-authorized handoff and require authoritative readback."""
    before = asana.get_task(task_gid)
    sections = _section_ids(before)
    existing_stories = asana.get_stories(task_gid)
    handoff_id = _identity(
        task_gid=task_gid, target_role=target_role, timestamp=timestamp, source=source
    )

    if in_progress_section_gid not in sections and ready_section_gid not in sections:
        raise HandoffError("owning task is neither Ready nor already In Progress")

    marker = (
        f"<!-- {HANDOFF_MARKER} handoff={handoff_id} task={task_gid} "
        f"role={target_role} at={_utc(timestamp).isoformat()} -->"
    )
    details = [
        marker,
        "AUTHORIZED IMPLEMENTATION HANDOFF",
        f"Task: {task_gid}",
        f"Target role: {target_role}",
        f"Handoff time: {_utc(timestamp).isoformat()}",
        f"Source: {source}",
        f"Branch: {branch or 'not yet known'}",
        f"Base: {base or 'not yet known'}",
        f"PR: {pr if pr is not None else 'not yet known'}",
        f"Head: {head or 'not yet known'}",
        "A missing PR/branch delta does not mean this handoff was not sent.",
        "— Dish Agent: Development Workflow | repository control plane",
    ]
    if not _has_handoff(existing_stories, handoff_id):
        asana.add_comment(task_gid, "\n".join(details))
    if in_progress_section_gid not in sections:
        move_task_to_section(asana, task_gid, in_progress_section_gid)

    after = asana.get_task(task_gid)
    after_stories = asana.get_stories(task_gid)
    if in_progress_section_gid not in _section_ids(after):
        raise HandoffError("handoff move did not read back in In Progress")
    if not _has_handoff(after_stories, handoff_id):
        raise HandoffError("handoff record did not read back")

    return HandoffRecord(
        handoff_id=handoff_id,
        task_gid=task_gid,
        target_role=target_role,
        timestamp=_utc(timestamp),
        source=source,
        branch=branch,
        base=base,
        pr=pr,
        head=head,
    )


def stale_handoff_due(
    record: HandoffRecord,
    *,
    now: datetime,
    authoritative_implementation_evidence: bool,
    stories: Iterable[Mapping[str, Any]],
) -> bool:
    if authoritative_implementation_evidence:
        return False
    if _utc(now) < record.timestamp + STALE_AFTER:
        return False
    return not _has_stale_notice(stories, record.handoff_id)


def record_stale_handoff_alert(
    *,
    asana: HandoffAsana,
    record: HandoffRecord,
    now: datetime,
    authoritative_implementation_evidence: bool,
) -> bool:
    """Record one observation-only stale alert.  Never reassign or redispatch."""
    stories = asana.get_stories(record.task_gid)
    if not stale_handoff_due(
        record,
        now=now,
        authoritative_implementation_evidence=authoritative_implementation_evidence,
        stories=stories,
    ):
        return False
    marker = f"<!-- {STALE_MARKER} handoff={record.handoff_id} -->"
    asana.add_comment(
        record.task_gid,
        "\n".join(
            (
                marker,
                "STALE HANDOFF — owner status unknown",
                f"Handoff {record.handoff_id} is at least three hours old and no authoritative implementation evidence is associated yet.",
                "Observation only: do not duplicate, replace, or redispatch without re-reading live Asana + GitHub lineage.",
                "— Dish Agent: Development Workflow | repository control plane",
            )
        ),
    )
    reread = asana.get_stories(record.task_gid)
    if not _has_stale_notice(reread, record.handoff_id):
        raise HandoffError("stale-handoff alert write did not read back")
    return True
