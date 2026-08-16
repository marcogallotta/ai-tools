"""Durable surfacing and resolution helpers for Marco-only Asana decisions.

This module keeps human decisions distinct from external blockers.  It never infers a
Marco decision from authenticated-account attribution: callers must supply the exact
current decision identity and explicit answer authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Any, Iterable, Mapping, Protocol

from operator_handoff import move_task_to_section


DECISION_MARKER = "dish-marco-decision:v1"
SURFACED_MARKER = "dish-marco-decision-surfaced:v1"
RESOLUTION_MARKER = "dish-marco-decision-resolution:v1"
REMIND_AFTER = timedelta(hours=24)
_DECISION_RE = re.compile(
    rf"^<!--\s*{re.escape(DECISION_MARKER)}\s+id=(?P<id>[0-9a-f]{{16}})\s+revision=(?P<rev>[A-Za-z0-9._-]+)\s*-->"
)
_SURFACE_RE = re.compile(
    rf"<!--\s*{re.escape(SURFACED_MARKER)}\s+id=(?P<id>[0-9a-f]{{16}})\s+kind=(?P<kind>initial|reminder)\s+at=(?P<at>[^\s]+)\s*-->"
)
_RESOLUTION_RE = re.compile(
    rf"<!--\s*{re.escape(RESOLUTION_MARKER)}\s+id=(?P<id>[0-9a-f]{{16}})\s+at=(?P<at>[^\s]+)\s*-->"
)
_REQUIRED_PACKET_LABELS = (
    "Decision needed:",
    "Recommended answer:",
    "Alternatives / material tradeoff:",
    "Consequence of no decision:",
    "What happens immediately after approval:",
)


class DecisionError(RuntimeError):
    pass


class DecisionAsana(Protocol):
    def get_task(self, gid: str) -> dict[str, Any]: ...
    def get_stories(self, gid: str) -> list[dict[str, Any]]: ...
    def add_comment(self, gid: str, text: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DecisionPacket:
    decision_id: str
    revision: str
    notes: str


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def decision_identity(*, task_gid: str, revision: str, question: str) -> str:
    raw = f"{task_gid}\0{revision}\0{question.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_decision_packet(task: Mapping[str, Any]) -> DecisionPacket | None:
    name = str(task.get("name") or "")
    notes = str(task.get("notes") or "")
    if not name.startswith("MARCO DECISION —"):
        return None
    match = _DECISION_RE.match(notes)
    if match is None:
        raise DecisionError("Marco-decision notes must begin with the exact decision marker")
    missing = [label for label in _REQUIRED_PACKET_LABELS if label not in notes]
    if missing:
        raise DecisionError(f"decision packet is incomplete: missing {', '.join(missing)}")
    return DecisionPacket(match.group("id"), match.group("rev"), notes)


def _surface_times(stories: Iterable[Mapping[str, Any]], decision_id: str) -> dict[str, datetime]:
    found: dict[str, datetime] = {}
    for story in stories:
        match = _SURFACE_RE.search(str(story.get("text") or ""))
        if not match or match.group("id") != decision_id:
            continue
        try:
            found[match.group("kind")] = datetime.fromisoformat(match.group("at").replace("Z", "+00:00"))
        except ValueError:
            raise DecisionError("decision surfaced marker has invalid timestamp")
    return found


def decision_surface_due(
    packet: DecisionPacket,
    *,
    stories: Iterable[Mapping[str, Any]],
    now: datetime,
) -> str | None:
    surfaces = _surface_times(stories, packet.decision_id)
    if "initial" not in surfaces:
        return "initial"
    if "reminder" in surfaces:
        return None
    if _utc(now) >= _utc(surfaces["initial"]) + REMIND_AFTER:
        return "reminder"
    return None


def record_decision_surface(
    *,
    asana: DecisionAsana,
    task_gid: str,
    now: datetime,
) -> str | None:
    task = asana.get_task(task_gid)
    packet = parse_decision_packet(task)
    if packet is None:
        return None
    stories = asana.get_stories(task_gid)
    kind = decision_surface_due(packet, stories=stories, now=now)
    if kind is None:
        return None
    marker = (
        f"<!-- {SURFACED_MARKER} id={packet.decision_id} kind={kind} "
        f"at={_utc(now).isoformat()} -->"
    )
    asana.add_comment(
        task_gid,
        "\n".join(
            (
                marker,
                f"Marco decision {kind} surface recorded for revision {packet.revision}.",
                "— Dish Agent: Coordinator | repository control plane",
            )
        ),
    )
    reread_task = asana.get_task(task_gid)
    reread_packet = parse_decision_packet(reread_task)
    if reread_packet is None or reread_packet.decision_id != packet.decision_id:
        raise DecisionError("decision revision changed during surfacing")
    if kind not in _surface_times(asana.get_stories(task_gid), packet.decision_id):
        raise DecisionError("decision surfaced state did not read back")
    return kind


def resolve_marco_decision(
    *,
    asana: DecisionAsana,
    task_gid: str,
    expected_decision_id: str,
    answer: str,
    next_section_gid: str,
    now: datetime,
) -> None:
    """Write an explicit answer, move lifecycle, and verify the exact revision cleared."""
    task = asana.get_task(task_gid)
    packet = parse_decision_packet(task)
    if packet is None or packet.decision_id != expected_decision_id:
        raise DecisionError("Marco answer does not bind to the current decision revision")
    if not answer.strip():
        raise DecisionError("Marco answer is empty")
    marker = f"<!-- {RESOLUTION_MARKER} id={packet.decision_id} at={_utc(now).isoformat()} -->"
    asana.add_comment(
        task_gid,
        "\n".join(
            (
                marker,
                f"MARCO DECISION RESOLVED — revision {packet.revision}",
                f"Answer: {answer.strip()}",
                "— Dish Agent: Coordinator | repository control plane | recording explicit Marco decision",
            )
        ),
    )
    move_task_to_section(asana, task_gid, next_section_gid)

    reread_task = asana.get_task(task_gid)
    memberships = reread_task.get("memberships") or []
    sections = {
        str(m.get("section", {}).get("gid"))
        for m in memberships
        if isinstance(m, Mapping) and isinstance(m.get("section"), Mapping)
    }
    if next_section_gid not in sections:
        raise DecisionError("resolved decision lifecycle move did not read back")
    if not any(
        (match := _RESOLUTION_RE.search(str(story.get("text") or "")))
        and match.group("id") == packet.decision_id
        for story in asana.get_stories(task_gid)
    ):
        raise DecisionError("resolved decision write did not read back")
