#!/usr/bin/env python3
"""Fenced append-only staged rollout authority for lifecycle-managed tasks."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping

from pr_lifecycle_support import AsanaREST, JSONHTTPClient, LifecycleError
from pr_lifecycle_task_state import (
    ROLLOUT_PLAN_PREFIX,
    ROLLOUT_TRANSITION_PREFIX,
    structured_story,
    structured_story_payload,
)

PLAN_SCHEMA = "dish-rollout-plan-v1"
PLAN_SCHEMA_V2 = "dish-rollout-plan-v2"
PLAN_SCHEMAS = {PLAN_SCHEMA, PLAN_SCHEMA_V2}
TRANSITION_SCHEMA = "dish-rollout-transition-v1"
TRANSITION_SCHEMA_V2 = "dish-rollout-transition-v2"
TRANSITION_SCHEMAS = {TRANSITION_SCHEMA, TRANSITION_SCHEMA_V2}
PROJECTION_START = "<!-- dish-rollout-projection:v1 -->"
PROJECTION_END = "<!-- /dish-rollout-projection:v1 -->"
HUMAN_EVENTS = {"ACCEPTED", "REJECTED"}
TERMINAL_EVENTS = HUMAN_EVENTS | {"CANCELLED"}
ALLOWED_EVENTS = {"ACTIVATED"} | TERMINAL_EVENTS
ALLOWED_EVENTS_V2 = ALLOWED_EVENTS | {"ROLLED_BACK"}
TRUSTED_HUMAN_PROVENANCE = {"marco-chat-exact-decision", "signed-operator-record"}


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise LifecycleError(f"rollout {label} is required")
    return text


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"rollout {label} is required and must be one string")
    return value.strip()


def _generation(value: Any) -> int:
    if isinstance(value, bool):
        raise LifecycleError("rollout generation must be a positive integer")
    try:
        generation = int(value)
    except (TypeError, ValueError) as exc:
        raise LifecycleError("rollout generation must be a positive integer") from exc
    if generation < 1:
        raise LifecycleError("rollout generation must be a positive integer")
    return generation


def _normalize_rollback(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise LifecycleError("rollout rollback must be an object or null")
    return {
        "state": _required_string(value.get("state"), "rollback state"),
        "artifact": _required_string(value.get("artifact"), "rollback artifact identity"),
        "config": _required_string(value.get("config"), "rollback config identity"),
        "activation_source": _required_string(value.get("activation_source"), "rollback activation source"),
        "activation_method": _required_string(value.get("activation_method"), "rollback activation method"),
    }


def _normalize_stage_v2(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {
        "stage": _required_string(item.get("stage"), "stage"),
        "artifact": _required_string(item.get("artifact"), "artifact identity"),
        "config": _required_string(item.get("config"), "config identity"),
        "target": _required_string(item.get("target"), f"stage {index + 1} target"),
        "scope": _required_string(item.get("scope"), f"stage {index + 1} scope"),
        "activation_source": _required_string(
            item.get("activation_source"), f"stage {index + 1} activation source"
        ),
        "activation_method": _required_string(
            item.get("activation_method"), f"stage {index + 1} activation method"
        ),
        "expected_prior_state": _required_string(
            item.get("expected_prior_state"), f"stage {index + 1} expected prior state"
        ),
        "fence": _required_string(item.get("fence"), f"stage {index + 1} fence identity"),
        "authority": _required_string(item.get("authority"), f"stage {index + 1} transition authority"),
        "rollback": _normalize_rollback(item.get("rollback")),
    }


def normalize_plan(task_gid: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    schema = str(raw.get("schema") or PLAN_SCHEMA)
    if schema not in PLAN_SCHEMAS:
        raise LifecycleError(f"unsupported rollout plan schema: {schema}")
    stages: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(raw.get("stages") or []):
        if not isinstance(item, Mapping):
            raise LifecycleError(f"rollout stage {index + 1} must be an object")
        if schema == PLAN_SCHEMA_V2:
            stage = _normalize_stage_v2(item, index)
        else:
            stage = {
                "stage": _required_text(item.get("stage"), "stage"),
                "artifact": _required_text(item.get("artifact"), "artifact identity"),
                "config": _required_text(item.get("config"), "config identity"),
            }
        name = stage["stage"]
        if name in names:
            raise LifecycleError(f"rollout stage is duplicated: {name}")
        names.add(name)
        stages.append(stage)
    if not stages:
        raise LifecycleError("rollout plan requires at least one stage")
    plan_id_value = raw.get("plan_id") or raw.get("control_id")
    plan_id = (
        _required_string(plan_id_value, "plan ID")
        if schema == PLAN_SCHEMA_V2
        else _required_text(plan_id_value, "plan ID")
    )
    plan: dict[str, Any] = {
        "schema": schema,
        "task_gid": _required_text(task_gid, "task GID"),
        "plan_id": plan_id,
        "generation": _generation(raw.get("generation")),
        "stages": stages,
        "predecessor_plan_digest": str(raw.get("predecessor_plan_digest") or "") or None,
    }
    if schema == PLAN_SCHEMA_V2:
        control_id = _required_string(raw.get("control_id") or plan_id, "control ID")
        if control_id != plan_id:
            raise LifecycleError("rollout control ID must match the durable plan ID")
        plan["control_id"] = control_id
    plan["plan_digest"] = _digest(plan)
    return plan


def transition_identity(raw: Mapping[str, Any]) -> str:
    keys = [
        "task_gid", "plan_id", "generation", "stage", "artifact", "config",
        "event", "activated_identity", "effect_mode", "human_decision",
    ]
    if raw.get("schema") == TRANSITION_SCHEMA_V2:
        # Post-effect readback is deliberately excluded: the transition identity is
        # the stable execution key needed before a retry-safe effect runs.
        keys.extend([
            "target", "scope", "activation_source", "activation_method",
            "fence", "authority", "prior_readback",
        ])
    stable = {key: raw.get(key) for key in keys}
    return hashlib.sha256(_canonical(stable).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class RolloutState:
    plans: tuple[dict[str, Any], ...]
    transitions: tuple[dict[str, Any], ...]

    @property
    def current_plan(self) -> dict[str, Any] | None:
        return max(self.plans, key=lambda item: int(item["generation"])) if self.plans else None


def reconstruct(stories: Iterable[Mapping[str, Any]], *, task_gid: str) -> RolloutState:
    plans_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    transitions_by_id: dict[str, dict[str, Any]] = {}
    for story in stories:
        plan = structured_story_payload(story, ROLLOUT_PLAN_PREFIX)
        if plan is not None:
            if plan.get("schema") not in PLAN_SCHEMAS or str(plan.get("task_gid")) != task_gid:
                continue
            normalized = normalize_plan(task_gid, plan)
            if normalized != plan:
                raise LifecycleError("rollout plan marker is not canonical")
            key = (str(plan["plan_id"]), int(plan["generation"]))
            prior = plans_by_key.get(key)
            if prior is not None and prior != plan:
                raise LifecycleError(f"conflicting rollout plan generation: {key[0]} generation {key[1]}")
            plans_by_key[key] = plan
        transition = structured_story_payload(story, ROLLOUT_TRANSITION_PREFIX)
        if transition is not None:
            if transition.get("schema") not in TRANSITION_SCHEMAS or str(transition.get("task_gid")) != task_gid:
                continue
            stable_id = str(transition.get("transition_id") or "")
            if not stable_id or transition_identity(transition) != stable_id:
                raise LifecycleError("rollout transition marker has invalid stable identity")
            prior = transitions_by_id.get(stable_id)
            if prior is not None and prior != transition:
                raise LifecycleError(f"conflicting rollout transition identity: {stable_id}")
            transitions_by_id[stable_id] = transition
    plans = tuple(sorted(plans_by_key.values(), key=lambda item: (str(item["plan_id"]), int(item["generation"]))))
    transitions = tuple(transitions_by_id.values())
    return RolloutState(plans, transitions)


@contextmanager
def rollout_fence(task_gid: str, plan_id: str, *, root: Path | None = None):
    root = (root or Path.home() / ".local" / "state" / "dish" / "rollout-fences").expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = hashlib.sha256(f"{task_gid}\0{plan_id}".encode()).hexdigest()
    path = root / f"{identity}.lock"
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LifecycleError(f"rollout committer is already active for task {task_gid} plan {plan_id}") from exc
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_readback(asana: Any, task_gid: str, prefix: str, payload: Mapping[str, Any]) -> None:
    body = structured_story(prefix, payload)
    asana.add_comment(task_gid, body)
    matches = [
        item for item in asana.get_stories(task_gid)
        if structured_story_payload(item, prefix) == payload
    ]
    if not matches:
        raise LifecycleError(f"rollout {prefix} append readback failed")


def install_plan(asana: Any, task_gid: str, raw: Mapping[str, Any], *, fence_root: Path | None = None) -> tuple[dict[str, Any], bool]:
    plan = normalize_plan(task_gid, raw)
    with rollout_fence(task_gid, plan["plan_id"], root=fence_root):
        state = reconstruct(asana.get_stories(task_gid), task_gid=task_gid)
        same = [item for item in state.plans if item["plan_id"] == plan["plan_id"] and item["generation"] == plan["generation"]]
        if same:
            if same[0] != plan:
                raise LifecycleError("rollout plan generation already exists with different exact identity")
            repair_projection(asana, task_gid, state)
            return plan, False
        current = state.current_plan
        if current is None:
            if plan["generation"] != 1 or plan["predecessor_plan_digest"] is not None:
                raise LifecycleError("first rollout plan must be generation 1 without a predecessor")
        else:
            if plan["plan_id"] != current["plan_id"]:
                raise LifecycleError("rollout plan ID cannot change across generations")
            if plan["generation"] != int(current["generation"]) + 1:
                raise LifecycleError("replacement rollout generation must advance exactly once")
            if plan["predecessor_plan_digest"] != current["plan_digest"]:
                raise LifecycleError("replacement rollout generation lacks the exact predecessor digest")
        _append_readback(asana, task_gid, ROLLOUT_PLAN_PREFIX, plan)
        updated = reconstruct(asana.get_stories(task_gid), task_gid=task_gid)
        repair_projection(asana, task_gid, updated)
        return plan, True


def _stage(plan: Mapping[str, Any], name: str) -> tuple[int, dict[str, Any]]:
    for index, item in enumerate(plan["stages"]):
        if item["stage"] == name:
            return index, dict(item)
    raise LifecycleError(f"rollout stage is not declared by the current generation: {name}")


def _stage_events(state: RolloutState, plan: Mapping[str, Any], stage: str) -> list[dict[str, Any]]:
    return [
        item for item in state.transitions
        if item["plan_id"] == plan["plan_id"]
        and item["generation"] == plan["generation"]
        and item["stage"] == stage
    ]


def _validate_human_decision(decision: Mapping[str, Any], transition: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {
        "decision": _required_text(decision.get("decision"), "human decision").upper(),
        "plan_id": _required_text(decision.get("plan_id"), "human decision plan ID"),
        "generation": _generation(decision.get("generation")),
        "stage": _required_text(decision.get("stage"), "human decision stage"),
        "activated_identity": _required_text(decision.get("activated_identity"), "activated identity"),
        "provenance": _required_text(decision.get("provenance"), "human decision provenance"),
        "source_id": _required_text(decision.get("source_id"), "human decision source ID"),
    }
    if normalized["provenance"] not in TRUSTED_HUMAN_PROVENANCE:
        raise LifecycleError("authenticated-account attribution alone is not human-decision provenance")
    if normalized["decision"] != transition["event"]:
        raise LifecycleError("human decision does not match the requested rollout event")
    for key in ("plan_id", "generation", "stage", "activated_identity"):
        if normalized[key] != transition[key]:
            raise LifecycleError(f"human decision is stale or mismatched at {key}")
    return normalized


def _normalize_prior_readback(value: Any, *, target: str, state: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise LifecycleError("rollout activation/rollback requires authoritative prior-state readback")
    normalized = {
        "target": _required_string(value.get("target"), "prior-state readback target"),
        "state": _required_string(value.get("state"), "prior-state readback state"),
        "evidence": _required_string(value.get("evidence"), "prior-state readback evidence"),
    }
    if normalized["target"] != target or normalized["state"] != state:
        raise LifecycleError("rollout prior-state readback does not match the expected target/state fence")
    return normalized


def _normalize_effective_readback(
    value: Any,
    *,
    target: str,
    state: str,
    artifact: str,
    config: str,
    activation_source: str,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise LifecycleError("rollout activation/rollback requires authoritative effective-state readback")
    normalized = {
        "target": _required_string(value.get("target"), "effective readback target"),
        "state": _required_string(value.get("state"), "effective readback state"),
        "artifact": _required_string(value.get("artifact"), "effective readback artifact identity"),
        "config": _required_string(value.get("config"), "effective readback config identity"),
        "activation_source": _required_string(value.get("activation_source"), "effective readback activation source"),
        "evidence": _required_string(value.get("evidence"), "effective readback evidence"),
    }
    expected = {
        "target": target,
        "state": state,
        "artifact": artifact,
        "config": config,
        "activation_source": activation_source,
    }
    for key, expected_value in expected.items():
        if normalized[key] != expected_value:
            raise LifecycleError(f"rollout effective readback does not match intended {key}")
    return normalized


def _effective_target(stage: Mapping[str, Any], event: str) -> Mapping[str, str]:
    if event == "ACTIVATED":
        return {
            "state": str(stage["stage"]),
            "artifact": str(stage["artifact"]),
            "config": str(stage["config"]),
            "activation_source": str(stage["activation_source"]),
        }
    rollback = stage.get("rollback")
    if not isinstance(rollback, Mapping):
        raise LifecycleError("rollout stage does not declare a supported rollback target")
    return rollback


def _latest_event(events: list[dict[str, Any]]) -> str | None:
    return str(events[-1]["event"]) if events else None


def commit_transition(
    asana: Any,
    task_gid: str,
    raw: Mapping[str, Any],
    *,
    effect: Callable[[str], None] | None = None,
    effect_readback: Callable[[str], bool | Mapping[str, Any]] | None = None,
    fence_root: Path | None = None,
    crash_after_append: bool = False,
) -> tuple[dict[str, Any], bool]:
    plan_id = _required_text(raw.get("plan_id") or raw.get("control_id"), "plan ID")
    with rollout_fence(task_gid, plan_id, root=fence_root):
        state = reconstruct(asana.get_stories(task_gid), task_gid=task_gid)
        plan = state.current_plan
        if plan is None or plan["plan_id"] != plan_id:
            raise LifecycleError("rollout transition has no current matching plan")
        v2 = plan["schema"] == PLAN_SCHEMA_V2
        generation = _generation(raw.get("generation"))
        if generation != plan["generation"]:
            raise LifecycleError("rollout transition targets a stale generation")
        stage_name = _required_text(raw.get("stage"), "stage")
        index, stage = _stage(plan, stage_name)
        event = _required_text(raw.get("event"), "event").upper()
        allowed = ALLOWED_EVENTS_V2 if v2 else ALLOWED_EVENTS
        if event not in allowed:
            raise LifecycleError(f"unsupported rollout event: {event}")
        if raw.get("artifact") != stage["artifact"] or raw.get("config") != stage["config"]:
            raise LifecycleError("rollout transition artifact/config identity does not match the plan")
        events = _stage_events(state, plan, stage_name)
        activation = next((item for item in events if item["event"] == "ACTIVATED"), None)
        activated_identity = str(raw.get("activated_identity") or "") or None
        prior_readback: dict[str, str] | None = None
        if event == "ACTIVATED":
            activated_identity = None
            if index:
                prior_name = plan["stages"][index - 1]["stage"]
                prior_events = _stage_events(state, plan, prior_name)
                if (v2 and _latest_event(prior_events) != "ACCEPTED") or (
                    not v2 and not any(item["event"] == "ACCEPTED" for item in prior_events)
                ):
                    raise LifecycleError(f"rollout predecessor stage is not accepted: {prior_name}")
            if v2:
                prior_readback = _normalize_prior_readback(
                    raw.get("prior_readback"), target=stage["target"], state=stage["expected_prior_state"]
                )
        else:
            if activation is None or activated_identity != activation["transition_id"]:
                raise LifecycleError("rollout decision is not bound to the exact activated identity")
            if v2 and event == "ROLLED_BACK":
                prior_readback = _normalize_prior_readback(
                    raw.get("prior_readback"), target=stage["target"], state=stage_name
                )
        transition: dict[str, Any] = {
            "schema": TRANSITION_SCHEMA_V2 if v2 else TRANSITION_SCHEMA,
            "task_gid": task_gid,
            "plan_id": plan_id,
            "generation": generation,
            "stage": stage_name,
            "artifact": stage["artifact"],
            "config": stage["config"],
            "event": event,
            "activated_identity": activated_identity,
            "effect_mode": None,
            "human_decision": None,
        }
        if v2:
            transition.update({
                "target": stage["target"],
                "scope": stage["scope"],
                "activation_source": stage["activation_source"],
                "activation_method": stage["activation_method"],
                "fence": stage["fence"],
                "authority": stage["authority"],
                "prior_readback": prior_readback,
                "readback": None,
            })
        automatic = bool(raw.get("automatic_effect"))
        if automatic:
            automatic_events = {"ACTIVATED", "ROLLED_BACK"} if v2 else {"ACTIVATED"}
            if event not in automatic_events:
                raise LifecycleError("automatic rollout effects are valid only for activation/rollback")
            effect_mode = str(raw.get("effect_mode") or "")
            if effect_mode not in {"idempotent-stable-key", "target-fenced"}:
                raise LifecycleError("automatic rollout effect requires declared idempotent-stable-key or target-fenced mode")
            transition["effect_mode"] = effect_mode
        if event in HUMAN_EVENTS:
            decision = raw.get("human_decision")
            if not isinstance(decision, Mapping):
                raise LifecycleError(f"rollout {event.lower()} requires an exact human decision record")
            transition["human_decision"] = _validate_human_decision(decision, transition)
        transition["transition_id"] = transition_identity(transition)
        existing = next((item for item in state.transitions if item["transition_id"] == transition["transition_id"]), None)
        if existing is not None:
            intent = {key: value for key, value in transition.items() if key != "readback"}
            durable_intent = {key: value for key, value in existing.items() if key != "readback"}
            if durable_intent != intent:
                raise LifecycleError("rollout transition identity conflicts with durable history")
            if v2 and not automatic and event in {"ACTIVATED", "ROLLED_BACK"}:
                target = _effective_target(stage, event)
                requested = _normalize_effective_readback(
                    raw.get("readback"),
                    target=stage["target"],
                    state=target["state"],
                    artifact=target["artifact"],
                    config=target["config"],
                    activation_source=target["activation_source"],
                )
                if existing.get("readback") != requested:
                    raise LifecycleError("rollout transition replay conflicts with durable effective readback")
            repair_projection(asana, task_gid, state)
            return existing, False
        if v2:
            latest = _latest_event(events)
            if event == "ACTIVATED" and events:
                raise LifecycleError("rollout stage is already activated or terminal")
            if event in TERMINAL_EVENTS and latest in TERMINAL_EVENTS | {"ROLLED_BACK"}:
                raise LifecycleError("rollout stage is already terminal")
            if event == "ROLLED_BACK" and latest not in {"ACTIVATED", "ACCEPTED", "REJECTED", "CANCELLED"}:
                raise LifecycleError("rollout rollback requires the exact activated stage to remain effectively active")
        elif events and any(item["event"] in TERMINAL_EVENTS for item in events):
            raise LifecycleError("rollout stage is already terminal")
        if automatic:
            if effect is None or effect_readback is None:
                raise LifecycleError("automatic rollout effect requires an idempotent adapter and authoritative readback")
            effect(transition["transition_id"])
            observed = effect_readback(transition["transition_id"])
            if v2:
                target = _effective_target(stage, event)
                transition["readback"] = _normalize_effective_readback(
                    observed,
                    target=stage["target"],
                    state=target["state"],
                    artifact=target["artifact"],
                    config=target["config"],
                    activation_source=target["activation_source"],
                )
            elif not observed:
                raise LifecycleError("automatic rollout effect did not pass authoritative readback")
        elif v2 and event in {"ACTIVATED", "ROLLED_BACK"}:
            target = _effective_target(stage, event)
            transition["readback"] = _normalize_effective_readback(
                raw.get("readback"),
                target=stage["target"],
                state=target["state"],
                artifact=target["artifact"],
                config=target["config"],
                activation_source=target["activation_source"],
            )
        _append_readback(asana, task_gid, ROLLOUT_TRANSITION_PREFIX, transition)
        if crash_after_append:
            raise RuntimeError("injected crash after authoritative transition append")
        updated = reconstruct(asana.get_stories(task_gid), task_gid=task_gid)
        repair_projection(asana, task_gid, updated)
        return transition, True


def rollout_projection(state: RolloutState) -> dict[str, Any] | None:
    plan = state.current_plan
    if plan is None:
        return None
    v2 = plan["schema"] == PLAN_SCHEMA_V2
    stages = []
    for item in plan["stages"]:
        events = _stage_events(state, plan, item["stage"])
        latest = events[-1] if events else None
        stage_projection = {
            **item,
            "state": latest["event"] if latest else "PENDING",
            "activated_identity": next((event["transition_id"] for event in events if event["event"] == "ACTIVATED"), None),
        }
        if v2:
            effective_event = next(
                (event for event in reversed(events) if event["event"] in {"ACTIVATED", "ROLLED_BACK"}),
                None,
            )
            readback = effective_event.get("readback") if effective_event else None
            stage_projection["effective_state"] = (
                readback.get("state") if isinstance(readback, Mapping) else "UNKNOWN"
            )
            stage_projection["effective_readback"] = readback
        stages.append(stage_projection)
    projection: dict[str, Any] = {
        "schema": "dish-rollout-projection-v2" if v2 else "dish-rollout-projection-v1",
        "task_gid": plan["task_gid"],
        "plan_id": plan["plan_id"],
        "generation": plan["generation"],
        "plan_digest": plan["plan_digest"],
        "superseded_generations": [item["generation"] for item in state.plans if item is not plan],
        "stages": stages,
        "complete": bool(stages) and all(item["state"] in {"ACCEPTED", "CANCELLED"} for item in stages),
    }
    if v2:
        projection["control_id"] = plan["control_id"]
    return projection


def repair_projection(asana: Any, task_gid: str, state: RolloutState | None = None) -> dict[str, Any] | None:
    state = state or reconstruct(asana.get_stories(task_gid), task_gid=task_gid)
    projection = rollout_projection(state)
    if projection is None:
        return None
    task = asana.get_task(task_gid)
    notes = str(task.get("notes") or "")
    block = PROJECTION_START + "\n" + json.dumps(projection, indent=2, sort_keys=True) + "\n" + PROJECTION_END
    pattern = re.compile(re.escape(PROJECTION_START) + r".*?" + re.escape(PROJECTION_END), re.DOTALL)
    desired = pattern.sub(block, notes) if pattern.search(notes) else notes.rstrip() + ("\n\n" if notes.strip() else "") + block + "\n"
    if desired != notes:
        asana.update_projection_fields(task_gid, {"notes": desired})
    readback = str(asana.get_task(task_gid).get("notes") or "")
    if block not in readback:
        raise LifecycleError("rollout notes projection readback failed; append-only transition history remains authoritative")
    return projection


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise LifecycleError("rollout request JSON must be an object")
    return dict(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asana-token", help=argparse.SUPPRESS)
    parser.add_argument("--http-timeout", type=float, default=10.0)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "repair"):
        command = sub.add_parser(name)
        command.add_argument("--task-gid", required=True)
    plan = sub.add_parser("install-plan")
    plan.add_argument("--task-gid", required=True)
    plan.add_argument("--request", required=True, type=Path)
    transition = sub.add_parser("transition")
    transition.add_argument("--task-gid", required=True)
    transition.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    token = args.asana_token or os.getenv("ASANA_ACCESS_TOKEN")
    if not token:
        parser.error("ASANA_ACCESS_TOKEN is required")
    asana = AsanaREST(token, http=JSONHTTPClient(timeout=args.http_timeout))
    try:
        if args.command == "install-plan":
            value, changed = install_plan(asana, args.task_gid, _load(args.request))
        elif args.command == "transition":
            request = _load(args.request)
            if request.get("automatic_effect"):
                raise LifecycleError("automatic effects require a repository adapter with authoritative readback")
            value, changed = commit_transition(asana, args.task_gid, request)
        else:
            state = reconstruct(asana.get_stories(args.task_gid), task_gid=args.task_gid)
            value = repair_projection(asana, args.task_gid, state) if args.command == "repair" else rollout_projection(state)
            changed = None
        print(json.dumps({"changed": changed, "rollout": value}, indent=2, sort_keys=True))
        return 0
    except (LifecycleError, OSError, ValueError) as exc:
        print(f"pr_lifecycle_rollout: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
