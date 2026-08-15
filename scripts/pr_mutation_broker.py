#!/usr/bin/env python3
"""GitHub-native serialized mutation admission for the Dish PR lifecycle.

This module is deliberately not a lifecycle engine. It consumes classifications from
``scripts/pr_lifecycle.py`` and turns eligible post-PR mutation requests into one
fenced grant per PR.  GitHub comments are the durable request/event record; an event
is authoritative only when an immutable proof artifact from the exact broker run
attempt binds that exact event comment and canonical event digest.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Protocol
from urllib import parse as urlparse
import uuid
import zipfile

from pr_lifecycle_owner import owning_task_identity_from_pr
from pr_lifecycle_support import FULL_SHA_RE, TASK_GID_RE, LifecycleError, LifecycleState
from pr_lifecycle_host_routing import LOCAL_IMPLEMENTATION, implementation_host_for_review
import pr_gate

REQUEST_MARKER = "dish-mutation-request:v1"
EVENT_MARKER = "dish-mutation-broker-event:v1"
PROOF_SCHEMA = "dish-mutation-broker-proof-v1"
EVENT_SCHEMA = "dish-mutation-broker-event-v1"
REQUEST_SCHEMA = "dish-mutation-request-v1"
WORKFLOW_PATH = ".github/workflows/pr-mutation-broker.yml"
PROOF_FILENAME = "mutation-broker-proof.json"
DEFAULT_STALE_AFTER = timedelta(minutes=60)
PROOF_RETENTION_DAYS = 7

MUTATION_ACTIONS = {
    "implementation",
    "fix",
    "integration-reconcile",
    "merge",
    "renew",
    "release",
    "complete",
    "takeover",
}
_NEW_GRANT_ACTIONS = {"implementation", "fix", "integration-reconcile", "merge"}
_CLOSE_ACTIONS = {"release", "complete"}
ROLE_FOR_ACTION = {
    "implementation": "implementation",
    "fix": "implementation",
    "integration-reconcile": "integration",
    "merge": "integration",
}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MARKER_RE = re.compile(r"<!--\s*dish-mutation-request:v1\s+(?P<fields>.*?)\s*-->", re.I | re.S)
_EVENT_RE = re.compile(r"<!--\s*dish-mutation-broker-event:v1\s+(?P<fields>.*?)\s*-->", re.I | re.S)
_ASANA_HOLD_RE = re.compile(
    r"(?im)^\s*(?:status|mutation\s+status)\s*:\s*(?:hold|blocked|not[_ -]?ready|paused)\b"
)
_ASANA_HOLD_MARKER_RE = re.compile(
    r"<!--\s*dish-mutation-hold:v1\s+state=(?:hold|blocked|paused)\s*-->", re.I
)
_MARCO_AUTHORITY_RE = re.compile(
    r"<!--\s*dish-marco-authority:v1\s+(?P<fields>.*?)\s*-->", re.I | re.S
)


class BrokerError(LifecycleError):
    """A broker request or proof failed a fail-closed admission rule."""


class BrokerProofError(BrokerError):
    """Current broker authority could not be proven from the exact run artifact."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise BrokerError("broker timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrokerError(f"invalid broker timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_request_body(body: str) -> str:
    # Line-ending normalization is the only content normalization.  Internal spacing,
    # prose and marker fields remain digest-bound so an edit cannot redefine authority.
    return body.replace("\r\n", "\n").replace("\r", "\n").strip()


def request_digest(body: str) -> str:
    return _digest_bytes(_normalized_request_body(body).encode("utf-8"))


def _marker_fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in raw.split():
        if "=" not in token:
            raise BrokerError("structured mutation marker contains a token without key=value")
        key, value = token.split("=", 1)
        key = key.strip().lower()
        if not key or key in fields:
            raise BrokerError("structured mutation marker contains duplicate/empty field")
        fields[key] = value.strip()
    return fields


def _optional_sha(value: str | None, *, field: str) -> str | None:
    if value in {None, "", "none"}:
        return None
    value = str(value).lower()
    if FULL_SHA_RE.fullmatch(value) is None:
        raise BrokerError(f"mutation request {field} must be a 40-character SHA or none")
    return value


def _optional_int(value: str | None, *, field: str) -> int | None:
    if value in {None, "", "none"}:
        return None
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise BrokerError(f"mutation request {field} must be numeric or none") from exc
    if parsed < 0:
        raise BrokerError(f"mutation request {field} must be non-negative")
    return parsed


def _optional_uuid(value: str | None, *, field: str) -> str | None:
    if value in {None, "", "none"}:
        return None
    value = str(value).lower()
    if _UUID_RE.fullmatch(value) is None:
        raise BrokerError(f"mutation request {field} must be a UUID or none")
    return value


@dataclass(frozen=True)
class MutationRequest:
    request_id: str
    action: str
    task_gid: str
    pr_number: int
    branch: str
    head: str
    review_id: int | None
    main_sha: str | None
    grant_id: str | None
    generation: int | None
    route: str
    authority_id: str | None
    comment_id: int
    comment_digest: str
    commenter: str
    created_at: str

    @property
    def identity(self) -> str:
        return f"{self.comment_id}:{self.comment_digest}:{self.request_id}"


def parse_request_comment(comment: Mapping[str, Any]) -> MutationRequest:
    body = str(comment.get("body") or "")
    matches = list(_MARKER_RE.finditer(body))
    if len(matches) != 1:
        raise BrokerError("mutation request comment must contain exactly one dish-mutation-request:v1 marker")
    fields = _marker_fields(matches[0].group("fields"))
    required = {"request", "action", "task", "pr", "branch", "head", "review", "main", "grant", "generation", "route"}
    missing = sorted(required - set(fields))
    if missing:
        raise BrokerError(f"mutation request is missing fields: {', '.join(missing)}")
    unknown = sorted(set(fields) - required - {"authority"})
    if unknown:
        raise BrokerError(f"mutation request contains unknown fields: {', '.join(unknown)}")
    request_id = fields["request"].lower()
    if _UUID_RE.fullmatch(request_id) is None:
        raise BrokerError("mutation request id must be a UUID")
    action = fields["action"].lower()
    if action not in MUTATION_ACTIONS:
        raise BrokerError(f"unsupported mutation action: {action}")
    task_gid = fields["task"]
    if TASK_GID_RE.fullmatch(task_gid) is None:
        raise BrokerError("mutation request task must be a 16-digit Asana GID")
    try:
        pr_number = int(fields["pr"])
        comment_id = int(comment["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BrokerError("mutation request PR/comment identity must be numeric") from exc
    if pr_number <= 0 or comment_id <= 0:
        raise BrokerError("mutation request PR/comment identity must be positive")
    branch = fields["branch"]
    if not branch or any(ch.isspace() for ch in branch):
        raise BrokerError("mutation request branch is invalid")
    head = _optional_sha(fields["head"], field="head")
    if head is None:
        raise BrokerError("mutation request head may not be none")
    route = fields["route"]
    if not route or any(ch.isspace() for ch in route):
        raise BrokerError("mutation request route is invalid")
    user = comment.get("user")
    commenter = str(user.get("login") if isinstance(user, Mapping) else comment.get("user_login") or "")
    if not commenter:
        raise BrokerError("mutation request commenter identity is missing")
    created = comment.get("created_at")
    created_at = _iso(_parse_time(created))
    return MutationRequest(
        request_id=request_id,
        action=action,
        task_gid=task_gid,
        pr_number=pr_number,
        branch=branch,
        head=head,
        review_id=_optional_int(fields["review"], field="review"),
        main_sha=_optional_sha(fields["main"], field="main"),
        grant_id=_optional_uuid(fields["grant"], field="grant"),
        generation=_optional_int(fields["generation"], field="generation"),
        route=route,
        authority_id=(None if fields.get("authority") in {None, "", "none"} else str(fields["authority"])),
        comment_id=comment_id,
        comment_digest=request_digest(body),
        commenter=commenter,
        created_at=created_at,
    )


def request_marker(
    *,
    request_id: str,
    action: str,
    task_gid: str,
    pr_number: int,
    branch: str,
    head: str,
    review_id: int | None = None,
    main_sha: str | None = None,
    grant_id: str | None = None,
    generation: int | None = None,
    route: str,
    authority_id: str | None = None,
) -> str:
    fields = [
        f"request={request_id}", f"action={action}", f"task={task_gid}", f"pr={pr_number}",
        f"branch={branch}", f"head={head}", f"review={review_id if review_id is not None else 'none'}",
        f"main={main_sha if main_sha is not None else 'none'}",
        f"grant={grant_id if grant_id is not None else 'none'}",
        f"generation={generation if generation is not None else 'none'}", f"route={route}",
    ]
    if authority_id is not None:
        fields.append(f"authority={authority_id}")
    return f"<!-- {REQUEST_MARKER} {' '.join(fields)} -->"


@dataclass(frozen=True)
class BrokerEvent:
    payload: dict[str, Any]
    event_digest: str
    comment_id: int
    proof_state: str
    artifact_id: int | None = None
    artifact_digest: str | None = None

    @property
    def kind(self) -> str:
        return str(self.payload["kind"])

    @property
    def grant_id(self) -> str:
        return str(self.payload["grant_id"])

    @property
    def generation(self) -> int:
        return int(self.payload["generation"])

    @property
    def stale_after(self) -> datetime:
        return _parse_time(self.payload["stale_after"])


def event_digest(payload: Mapping[str, Any]) -> str:
    return _digest_bytes(_canonical_json(payload))


def _payload_token(payload: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(_canonical_json(payload)).decode("ascii").rstrip("=")


def _decode_payload(token: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode((token + padding).encode("ascii"))
        value = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise BrokerProofError("broker event payload is not valid canonical base64 JSON") from exc
    if not isinstance(value, dict):
        raise BrokerProofError("broker event payload must decode to a JSON object")
    return value


def _event_marker_fields(body: str) -> dict[str, str] | None:
    matches = list(_EVENT_RE.finditer(body or ""))
    if not matches:
        return None
    if len(matches) != 1:
        raise BrokerProofError("broker event comment must contain exactly one event marker")
    return _marker_fields(matches[0].group("fields"))


def parse_event_comment(comment: Mapping[str, Any]) -> BrokerEvent | None:
    fields = _event_marker_fields(str(comment.get("body") or ""))
    if fields is None:
        return None
    required = {"payload", "digest", "proof"}
    if not required.issubset(fields):
        raise BrokerProofError("broker event marker is missing payload/digest/proof")
    allowed = required | {"artifact_id", "artifact_digest"}
    if set(fields) - allowed:
        raise BrokerProofError("broker event marker contains unknown proof transport fields")
    payload = _decode_payload(fields["payload"])
    digest = fields["digest"].lower()
    if _DIGEST_RE.fullmatch(digest) is None or event_digest(payload) != digest:
        raise BrokerProofError("BROKER PROOF INVALID: broker event digest mismatch")
    if payload.get("schema") != EVENT_SCHEMA:
        raise BrokerProofError("BROKER PROOF INVALID: unsupported broker event schema")
    try:
        comment_id = int(comment["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BrokerProofError("BROKER PROOF INVALID: broker event comment id missing") from exc
    proof = fields["proof"].upper()
    if proof not in {"PENDING", "COMPLETE"}:
        raise BrokerProofError("BROKER PROOF INVALID: broker proof state is invalid")
    artifact_id = None
    artifact_digest = None
    if proof == "COMPLETE":
        try:
            artifact_id = int(fields["artifact_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerProofError("BROKER PROOF INVALID: COMPLETE event lacks artifact id") from exc
        artifact_digest = fields.get("artifact_digest")
        if not artifact_digest or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest.lower()):
            raise BrokerProofError("BROKER PROOF INVALID: COMPLETE event lacks SHA-256 artifact digest")
        artifact_digest = artifact_digest.lower()
    return BrokerEvent(
        payload=payload,
        event_digest=digest,
        comment_id=comment_id,
        proof_state=proof,
        artifact_id=artifact_id,
        artifact_digest=artifact_digest,
    )


def artifact_name(run_id: int | str, run_attempt: int | str, comment_id: int | str) -> str:
    return f"mutation-broker-proof-r{int(run_id)}-a{int(run_attempt)}-c{int(comment_id)}"


def proof_payload(event: BrokerEvent) -> dict[str, Any]:
    p = event.payload
    return {
        "schema": PROOF_SCHEMA,
        "repository": p["repository"],
        "repository_id": str(p["repository_id"]),
        "comment_id": event.comment_id,
        "event_digest": event.event_digest,
        "request_comment_id": int(p["request_comment_id"]),
        "request_digest": p["request_digest"],
        "request_id": p["request_id"],
        "event_id": p["event_id"],
        "grant_id": p["grant_id"],
        "generation": int(p["generation"]),
        "action": p["action"],
        "workflow_path": p["workflow_path"],
        "trusted_source_sha": p["trusted_source_sha"],
        "run_id": int(p["run_id"]),
        "run_attempt": int(p["run_attempt"]),
    }


def provisional_event_comment(event: BrokerEvent) -> str:
    p = event.payload
    marker = (
        f"<!-- {EVENT_MARKER} payload={_payload_token(p)} digest={event.event_digest} proof=PENDING -->"
    )
    return (
        f"{marker}\nBROKER EVENT PROVISIONAL — **NON-AUTHORITATIVE** until the exact run-attempt proof "
        f"artifact is uploaded, written back to this same comment, and independently verified.\n\n"
        f"Action: `{p['action']}` · PR #{p['pr_number']} · head `{p['starting_head']}` · "
        f"grant `{p['grant_id']}` generation `{p['generation']}`.\n\n— Dish mutation broker"
    )


def complete_event_comment(event: BrokerEvent, *, artifact_id: int, artifact_digest: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest.lower()):
        raise BrokerError("artifact digest must be sha256:<64hex>")
    p = event.payload
    marker = (
        f"<!-- {EVENT_MARKER} payload={_payload_token(p)} digest={event.event_digest} proof=COMPLETE "
        f"artifact_id={int(artifact_id)} artifact_digest={artifact_digest.lower()} -->"
    )
    return (
        f"{marker}\nBROKER EVENT — proof transport recorded for exact run `{p['run_id']}` attempt "
        f"`{p['run_attempt']}`. Authority still requires an independent parser to verify that exact attempt "
        "completed successfully and that its unique artifact matches this comment.\n\n"
        f"Action: `{p['action']}` · PR #{p['pr_number']} · head `{p['starting_head']}` · "
        f"grant `{p['grant_id']}` generation `{p['generation']}`.\n\n— Dish mutation broker"
    )


class BrokerGitHub(Protocol):
    repository: str

    def get_workflow_run_attempt(self, run_id: int, run_attempt: int) -> dict[str, Any]: ...
    def get_run_artifacts(self, run_id: int) -> list[dict[str, Any]]: ...
    def download_artifact(self, artifact_id: int) -> bytes: ...


def _proof_file_from_zip(archive: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive), "r") as zf:
            names = [name for name in zf.namelist() if not name.endswith("/")]
            if names != [PROOF_FILENAME]:
                raise BrokerProofError(
                    f"BROKER PROOF INVALID: artifact must contain exactly {PROOF_FILENAME!r}; got {names!r}"
                )
            value = json.loads(zf.read(PROOF_FILENAME))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        if isinstance(exc, BrokerProofError):
            raise
        raise BrokerProofError("BROKER PROOF INVALID: proof artifact is unreadable") from exc
    if not isinstance(value, dict):
        raise BrokerProofError("BROKER PROOF INVALID: proof file must be a JSON object")
    return value


def verify_event_proof(
    github: BrokerGitHub,
    event: BrokerEvent,
    *,
    expected_repository: str,
    expected_repository_id: int | str,
) -> BrokerEvent:
    """Verify exact comment/run-attempt/artifact/content binding for one broker event."""
    if event.proof_state != "COMPLETE" or event.artifact_id is None or event.artifact_digest is None:
        raise BrokerProofError("BROKER PROOF INVALID / RECOVERY REQUIRED: event proof is incomplete")
    p = event.payload
    if p.get("repository") != expected_repository or str(p.get("repository_id")) != str(expected_repository_id):
        raise BrokerProofError("BROKER PROOF INVALID: repository identity mismatch")
    run_id = int(p["run_id"])
    run_attempt = int(p["run_attempt"])
    run = github.get_workflow_run_attempt(run_id, run_attempt)
    if int(run.get("id", -1)) != run_id or int(run.get("run_attempt", -1)) != run_attempt:
        raise BrokerProofError("BROKER PROOF INVALID: exact workflow run attempt identity mismatch")
    if str(run.get("event") or "") != "issue_comment" or str(run.get("conclusion") or "") != "success":
        raise BrokerProofError("BROKER PROOF INVALID: exact broker run attempt is not successful issue_comment")
    if str(run.get("path") or run.get("workflow_path") or "") != str(p["workflow_path"]):
        raise BrokerProofError("BROKER PROOF INVALID: workflow path mismatch")
    if str(p["workflow_path"]) != WORKFLOW_PATH:
        raise BrokerProofError("BROKER PROOF INVALID: event does not reference the trusted broker workflow")
    if str(run.get("head_sha") or "").lower() != str(p["trusted_source_sha"]).lower():
        raise BrokerProofError("BROKER PROOF INVALID: trusted broker source SHA mismatch")
    run_repo = run.get("repository")
    if isinstance(run_repo, Mapping) and str(run_repo.get("id")) != str(expected_repository_id):
        raise BrokerProofError("BROKER PROOF INVALID: workflow run repository mismatch")

    expected_name = artifact_name(run_id, run_attempt, event.comment_id)
    artifacts = [
        a for a in github.get_run_artifacts(run_id)
        if str(a.get("name") or "") == expected_name and not bool(a.get("expired"))
    ]
    if len(artifacts) != 1:
        raise BrokerProofError(
            "BROKER PROOF INVALID / RECOVERY REQUIRED: exact proof artifact is missing, expired, or duplicated"
        )
    artifact = artifacts[0]
    workflow_run = artifact.get("workflow_run")
    if isinstance(workflow_run, Mapping) and int(workflow_run.get("id", -1)) != run_id:
        raise BrokerProofError("BROKER PROOF INVALID: proof artifact belongs to another run")
    if int(artifact.get("id", -1)) != event.artifact_id:
        raise BrokerProofError("BROKER PROOF INVALID: proof artifact id mismatch")
    if str(artifact.get("digest") or "").lower() != event.artifact_digest:
        raise BrokerProofError("BROKER PROOF INVALID: proof artifact digest metadata mismatch")

    archive = github.download_artifact(event.artifact_id)
    # upload-artifact v4's digest is a SHA-256 archive digest. If the downloaded archive
    # is represented identically, enforce it too; tests exercise this binding.  GitHub
    # metadata remains mandatory in either case.
    actual_archive_digest = f"sha256:{_digest_bytes(archive)}"
    if actual_archive_digest != event.artifact_digest:
        raise BrokerProofError("BROKER PROOF INVALID: downloaded artifact SHA-256 mismatch")
    proof = _proof_file_from_zip(archive)
    expected_proof = proof_payload(event)
    if proof != expected_proof:
        raise BrokerProofError("BROKER PROOF INVALID: proof content does not match current event comment")
    return event


@dataclass(frozen=True)
class GrantState:
    grant_id: str
    generation: int
    action: str
    task_gid: str
    pr_number: int
    branch: str
    starting_head: str
    review_id: int | None
    main_sha: str | None
    route: str
    consumer_id: str
    issued_at: datetime
    stale_after: datetime
    event_comment_id: int
    closed: bool = False

    def is_stale(self, now: datetime | None = None) -> bool:
        return (now or _now()) >= self.stale_after


def fold_verified_events(events: Iterable[BrokerEvent]) -> GrantState | None:
    """Fold proven state events, failing closed on malformed generations/transitions."""
    ordered = sorted(events, key=lambda e: (int(e.payload.get("generation", -1)), e.comment_id))
    state: GrantState | None = None
    seen_event_ids: set[str] = set()
    for event in ordered:
        p = event.payload
        event_id = str(p.get("event_id") or "")
        if not event_id or event_id in seen_event_ids:
            raise BrokerProofError("BROKER PROOF INVALID: duplicate/missing broker event identity")
        seen_event_ids.add(event_id)
        kind = str(p.get("kind") or "")
        generation = int(p.get("generation", -1))
        grant_id = str(p.get("grant_id") or "")
        if kind in {"grant", "takeover"}:
            if state is not None and not state.closed and generation <= state.generation:
                raise BrokerProofError("BROKER PROOF INVALID: overlapping active grant generations")
            if state is not None and not state.closed and generation > state.generation:
                # A new generation may only supersede an old unclosed one through an explicit
                # proven takeover event, never ordinary age-based reassignment.
                if kind != "takeover":
                    raise BrokerProofError("BROKER PROOF INVALID: grant generation advanced without proven takeover/close")
            state = GrantState(
                grant_id=grant_id,
                generation=generation,
                action=str(p["action"]),
                task_gid=str(p["task_gid"]),
                pr_number=int(p["pr_number"]),
                branch=str(p["branch"]),
                starting_head=str(p["starting_head"]),
                review_id=(None if p.get("review_id") is None else int(p["review_id"])),
                main_sha=(None if p.get("main_sha") is None else str(p["main_sha"])),
                route=str(p["route"]),
                consumer_id=str(p["consumer_id"]),
                issued_at=_parse_time(p["issued_at"]),
                stale_after=_parse_time(p["stale_after"]),
                event_comment_id=event.comment_id,
                closed=False,
            )
            continue
        if kind not in {"renew", "close"}:
            raise BrokerProofError(f"BROKER PROOF INVALID: unknown state event kind {kind!r}")
        if state is None or state.closed:
            raise BrokerProofError(f"BROKER PROOF INVALID: {kind} without a current grant")
        if grant_id != state.grant_id or generation != state.generation:
            raise BrokerProofError(f"BROKER PROOF INVALID: {kind} does not bind current grant generation")
        if str(p["route"]) != state.route or str(p["consumer_id"]) != state.consumer_id:
            raise BrokerProofError(f"BROKER PROOF INVALID: {kind} changed accepted consumer/route")
        if kind == "renew":
            state = replace(
                state,
                stale_after=_parse_time(p["stale_after"]),
                event_comment_id=event.comment_id,
            )
        else:
            state = replace(state, closed=True, event_comment_id=event.comment_id)
    return state


def route_policy_from_json(raw: str | None) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrokerError("DISH_MUTATION_BROKER_ALLOWED_ROUTES_JSON is invalid JSON") from exc
    if not isinstance(value, dict):
        raise BrokerError("broker allowed-routes configuration must be a JSON object")
    out: dict[str, dict[str, Any]] = {}
    for route, config in value.items():
        if not isinstance(route, str) or not route or not isinstance(config, dict):
            raise BrokerError("broker route policy entry is invalid")
        role = str(config.get("role") or "")
        actions = config.get("actions")
        if role not in {"implementation", "integration"} or not isinstance(actions, list):
            raise BrokerError(f"broker route {route!r} must declare implementation/integration role and actions")
        normalized = [str(action).lower() for action in actions]
        if any(action not in MUTATION_ACTIONS for action in normalized):
            raise BrokerError(f"broker route {route!r} declares unsupported action")
        host = str(config.get("host") or ("chatgpt" if role == "implementation" else "")).lower()
        if role == "implementation" and host not in {"chatgpt", "local"}:
            raise BrokerError(f"broker implementation route {route!r} must declare host=chatgpt|local")
        out[route] = {"role": role, "actions": normalized}
        if host:
            out[route]["host"] = host
    return out


def route_authorized(policy: Mapping[str, Mapping[str, Any]], route: str, action: str) -> bool:
    config = policy.get(route)
    if not isinstance(config, Mapping):
        return False
    if action in {"renew", "release", "complete", "takeover"}:
        return action in set(map(str, config.get("actions", [])))
    return str(config.get("role")) == ROLE_FOR_ACTION.get(action) and action in set(map(str, config.get("actions", [])))


def asana_task_allows_mutation(task: Mapping[str, Any]) -> tuple[bool, str | None]:
    if bool(task.get("completed")):
        return False, "owning Asana task is complete"
    notes = str(task.get("notes") or "")
    if _ASANA_HOLD_RE.search(notes) or _ASANA_HOLD_MARKER_RE.search(notes):
        return False, "live Asana authority places the owning task on hold/not-ready"
    return True, None


def marco_authority_present(task: Mapping[str, Any], authority_id: str | None, *, action: str) -> bool:
    if not authority_id:
        return False
    for match in _MARCO_AUTHORITY_RE.finditer(str(task.get("notes") or "")):
        fields = _marker_fields(match.group("fields"))
        if fields.get("decision") == authority_id and fields.get("action") == action:
            return fields.get("broker_admission", "").lower() in {"1", "true", "yes", "waived"}
    return False


def consumer_id(repository: str, pr_number: int, grant_id: str, generation: int, route: str) -> str:
    value = f"dish-broker-consumer:v1:{repository}:{pr_number}:{grant_id}:{generation}:{route}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_event_payload(
    *,
    repository: str,
    repository_id: int | str,
    kind: str,
    request: MutationRequest,
    grant_id: str,
    generation: int,
    action: str,
    branch: str,
    starting_head: str,
    review_id: int | None,
    main_sha: str | None,
    route: str,
    run_id: int,
    run_attempt: int,
    trusted_source_sha: str,
    issued_at: datetime,
    stale_after: datetime,
    event_id: str | None = None,
    outcome: str = "accepted",
) -> dict[str, Any]:
    if kind not in {"grant", "renew", "close", "takeover"}:
        raise BrokerError("invalid authoritative broker event kind")
    if FULL_SHA_RE.fullmatch(starting_head) is None or FULL_SHA_RE.fullmatch(trusted_source_sha) is None:
        raise BrokerError("broker event requires exact starting/trusted source SHAs")
    return {
        "schema": EVENT_SCHEMA,
        "repository": repository,
        "repository_id": str(repository_id),
        "kind": kind,
        "request_comment_id": request.comment_id,
        "request_digest": request.comment_digest,
        "request_id": request.request_id,
        "event_id": event_id or str(uuid.uuid4()),
        "grant_id": grant_id,
        "generation": int(generation),
        "action": action,
        "task_gid": request.task_gid,
        "pr_number": request.pr_number,
        "branch": branch,
        "starting_head": starting_head,
        "review_id": review_id,
        "main_sha": main_sha,
        "route": route,
        "consumer_id": consumer_id(repository, request.pr_number, grant_id, generation, route),
        "run_id": int(run_id),
        "run_attempt": int(run_attempt),
        "workflow_path": WORKFLOW_PATH,
        "trusted_source_sha": trusted_source_sha,
        "issued_at": _iso(issued_at),
        "stale_after": _iso(stale_after),
        "outcome": outcome,
    }


def event_from_payload(payload: dict[str, Any], *, comment_id: int = 0, proof_state: str = "PENDING") -> BrokerEvent:
    return BrokerEvent(
        payload=payload,
        event_digest=event_digest(payload),
        comment_id=int(comment_id),
        proof_state=proof_state,
    )


def write_proof(path: str | Path, event: BrokerEvent) -> None:
    Path(path).write_text(json.dumps(proof_payload(event), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def broker_filter_event(event: Mapping[str, Any]) -> tuple[bool, int | None, int | None]:
    """Cheap request-only workflow filter. No GitHub/Asana reads happen here."""
    issue = event.get("issue")
    comment = event.get("comment")
    if not isinstance(issue, Mapping) or not issue.get("pull_request") or not isinstance(comment, Mapping):
        return False, None, None
    body = str(comment.get("body") or "")
    if len(list(_MARKER_RE.finditer(body))) != 1:
        return False, None, None
    try:
        request = parse_request_comment(comment)
        issue_number = int(issue.get("number"))
    except (BrokerError, TypeError, ValueError):
        return False, None, None
    if request.pr_number != issue_number:
        return False, None, None
    return True, issue_number, request.comment_id


def authoritative_events_from_comments(
    github: BrokerGitHub,
    comments: Iterable[Mapping[str, Any]],
    *,
    repository: str,
    repository_id: int | str,
) -> list[BrokerEvent]:
    events: list[BrokerEvent] = []
    for comment in comments:
        parsed = parse_event_comment(comment)
        if parsed is None:
            continue
        # Provisional comments are deliberately non-authoritative and ignored as state.
        if parsed.proof_state == "PENDING":
            continue
        events.append(
            verify_event_proof(
                github,
                parsed,
                expected_repository=repository,
                expected_repository_id=repository_id,
            )
        )
    return events


def validate_live_request_preconditions(
    request: MutationRequest,
    *,
    pr: Mapping[str, Any],
    task: Mapping[str, Any],
    permission: str,
    route_policy: Mapping[str, Mapping[str, Any]],
) -> None:
    try:
        number = int(pr["number"])
        head = str(pr["head"]["sha"]).lower()
        branch = str(pr["head"]["ref"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BrokerError("live pull request identity is incomplete") from exc
    if request.action not in _CLOSE_ACTIONS and (
        str(pr.get("state") or "").lower() != "open" or bool(pr.get("merged"))
    ):
        raise BrokerError("pull request is not open for mutation")
    if number != request.pr_number or head != request.head or branch != request.branch:
        raise BrokerError("mutation request optimistic PR/branch/head preconditions are stale")
    owner, owner_error = owning_task_identity_from_pr(pr)
    if owner_error or owner != request.task_gid:
        raise BrokerError(f"mutation request does not match explicit owning task: {owner_error or owner!r}")
    if str(task.get("gid") or request.task_gid) != request.task_gid:
        raise BrokerError("live Asana task identity mismatch")
    allowed, reason = asana_task_allows_mutation(task)
    if not allowed:
        raise BrokerError(reason or "live Asana task does not permit mutation")
    if permission.lower() not in {"write", "maintain", "admin"}:
        raise BrokerError("requester lacks required repository collaborator permission")
    if request.action != "takeover" and not route_authorized(route_policy, request.route, request.action):
        raise BrokerError("configured route does not carry the standing role/action authority required for this request")


def grant_matches_request(state: GrantState, request: MutationRequest) -> bool:
    return (
        request.grant_id == state.grant_id
        and request.generation == state.generation
        and request.pr_number == state.pr_number
        and request.task_gid == state.task_gid
        and request.route == state.route
    )


def takeover_matches_current_grant(state: GrantState, request: MutationRequest) -> bool:
    """Takeover replaces one fenced mutation only; it never reclassifies that mutation."""
    return (
        grant_matches_request(state, request)
        and request.branch == state.branch
        and request.head == state.starting_head
    )


def validate_takeover_preconditions(
    request: MutationRequest,
    *,
    current: GrantState | None,
    route_policy: Mapping[str, Mapping[str, Any]],
) -> None:
    if current is None or current.closed or not takeover_matches_current_grant(current, request):
        raise BrokerError("takeover does not reference the exact current grant generation/branch/head")
    # Route changes need their own explicit durable authority. V1 has none, so the
    # replacement must stay on the fenced route and prove that route carries the
    # original grant action, rather than merely declaring the takeover verb.
    if request.route != current.route or not route_authorized(route_policy, request.route, current.action):
        raise BrokerError("takeover route does not carry the current grant's standing role/action authority")


def decision_for_request(
    request: MutationRequest,
    *,
    current: GrantState | None,
    task: Mapping[str, Any],
    now: datetime,
) -> tuple[str, str, int]:
    """Return (kind, grant_id, generation); callers still enforce lifecycle eligibility."""
    if request.action in _NEW_GRANT_ACTIONS:
        if current is not None and not current.closed:
            raise BrokerError("BUSY: a current mutation grant already fences this PR")
        generation = 1 if current is None else current.generation + 1
        return "grant", str(uuid.uuid4()), generation
    if request.action == "renew":
        if current is None or current.closed or not grant_matches_request(current, request):
            raise BrokerError("renew does not reference the current grant generation/route")
        if request.head != current.starting_head:
            raise BrokerError("renew cannot transfer a grant across head movement")
        return "renew", current.grant_id, current.generation
    if request.action in _CLOSE_ACTIONS:
        if current is None or current.closed or not grant_matches_request(current, request):
            raise BrokerError("close does not reference the current grant generation/route")
        return "close", current.grant_id, current.generation
    if request.action == "takeover":
        if current is None or current.closed or not current.is_stale(now):
            raise BrokerError("takeover requires a stale current grant; age alone never transfers work")
        if not takeover_matches_current_grant(current, request):
            raise BrokerError("takeover does not reference the exact current grant generation/branch/head")
        if not marco_authority_present(task, request.authority_id, action="takeover"):
            raise BrokerError("takeover requires exact durable Marco authority naming broker admission")
        return "takeover", str(uuid.uuid4()), current.generation + 1
    raise BrokerError(f"unsupported broker decision action: {request.action}")


def action_for_state_event(request: MutationRequest, current: GrantState | None) -> str:
    if request.action in {"renew", "release", "complete", "takeover"} and current is not None:
        return current.action
    return request.action


def proof_zip_bytes(proof: Mapping[str, Any]) -> bytes:
    """Deterministic helper used by tests/fakes to model upload-artifact proof archives."""
    buffer = io.BytesIO()
    # ZIP timestamp cannot predate 1980; use a constant so test digest is stable.
    info = zipfile.ZipInfo(PROOF_FILENAME, (2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(info, json.dumps(dict(proof), indent=2, sort_keys=True) + "\n")
    return buffer.getvalue()


def _review_id(review: Mapping[str, Any] | None) -> int | None:
    if not isinstance(review, Mapping):
        return None
    try:
        return int(review.get("id"))
    except (TypeError, ValueError):
        return None


def validate_lifecycle_eligibility(
    request: MutationRequest,
    *,
    lifecycle: Any,
    reviews: Iterable[Mapping[str, Any]],
    live_main_sha: str,
    integration_authority: bool,
    current_grant: GrantState | None,
    route_policy: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Consume the existing lifecycle decision; never manufacture role/lifecycle authority here."""
    if request.action in {"release", "complete"}:
        # Closing admission is allowed after the consumer's mutation moved lifecycle/head;
        # grant identity/route is validated independently by decision_for_request().
        return
    if request.action == "takeover":
        # Positive Marco authority + stale current grant is enforced separately.  The
        # replacement still resumes the original action and consumers revalidate before mutation.
        return

    exact_review = pr_gate.latest_exact_head_review(list(reviews), reviewed_head=request.head)
    exact_review_id = _review_id(exact_review)
    if request.action == "implementation":
        if lifecycle.state != LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
            raise BrokerError("lifecycle does not currently authorize post-PR Implementation continuation")
        return

    if request.action == "fix":
        if lifecycle.state != LifecycleState.CHANGES_REQUESTED:
            raise BrokerError("lifecycle does not currently authorize a fix mutation")
        formal_block = bool(exact_review and exact_review.get("verdict") == "BLOCK")
        pr_owned_ci_failure = bool(
            lifecycle.gate
            and lifecycle.gate.get("diagnosis") == pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value
            and lifecycle.gate.get("failure_ownership") == "PR_OWNED"
            and lifecycle.external_dependency is None
        )
        if not formal_block and not pr_owned_ci_failure:
            raise BrokerError("fix requires the current formal BLOCK or proven PR-owned CI failure")
        if exact_review_id is not None and request.review_id != exact_review_id:
            raise BrokerError("fix request is not bound to the current exact-head formal Review id")
        if formal_block and request.review_id is None:
            raise BrokerError("formal BLOCK fix admission requires exact (head, block_review_id) identity")
        policy = route_policy or {}
        route_config = policy.get(request.route) if isinstance(policy, Mapping) else None
        route_host = str(route_config.get("host") or "chatgpt") if isinstance(route_config, Mapping) else "chatgpt"
        if route_host == "local":
            if not formal_block or implementation_host_for_review(exact_review) != LOCAL_IMPLEMENTATION:
                raise BrokerError(
                    "local Implementation fix route requires exact Review classification proving the unavailable "
                    "remote capability and exhausted fallbacks"
                )
        return

    if request.action in {"integration-reconcile", "merge"}:
        if not integration_authority:
            raise BrokerError("standing bounded Integration authority is not enabled")
        if not exact_review or exact_review.get("verdict") != "MERGE" or request.review_id != exact_review_id:
            raise BrokerError("Integration mutation requires the current exact-head formal MERGE Review id")
        if request.main_sha != live_main_sha:
            raise BrokerError("Integration request main precondition is stale or missing")
        if request.action == "merge":
            if lifecycle.state != LifecycleState.INTEGRATION_READY:
                raise BrokerError("lifecycle gates do not currently authorize merge")
            return
        # Reconciliation is intentionally narrower than generic Integration. It is
        # available only when an exact reviewed head is blocked before merge by an
        # integration/base/mergeability condition. Any semantic ambiguity remains a
        # non-eligible lifecycle state and returns to Implementation.
        if lifecycle.state != LifecycleState.REVIEW_PASSED:
            raise BrokerError("lifecycle does not currently require Integration reconciliation")
        reason = str(lifecycle.residual_reason or "").lower()
        if not any(token in reason for token in ("mergeab", "integration ordering", "base", "conflict")):
            raise BrokerError("no bounded head-changing Integration reconciliation requirement is proven")
        return

    if request.action == "renew":
        if current_grant is None or current_grant.closed:
            raise BrokerError("renew requires a current grant")
        original = current_grant.action
        if original == "implementation" and lifecycle.state != LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED:
            raise BrokerError("Implementation grant is no longer lifecycle-current")
        if original == "fix" and lifecycle.state != LifecycleState.CHANGES_REQUESTED:
            raise BrokerError("fix grant is no longer lifecycle-current")
        if original == "merge" and lifecycle.state not in {LifecycleState.INTEGRATION_READY, LifecycleState.MERGING}:
            raise BrokerError("merge grant is no longer lifecycle-current")
        if original == "integration-reconcile" and lifecycle.state != LifecycleState.REVIEW_PASSED:
            raise BrokerError("reconciliation grant is no longer lifecycle-current")
        return
    raise BrokerError(f"no lifecycle eligibility rule for {request.action!r}")


def prepare_broker_event(
    *,
    engine: Any,
    request_comment_id: int,
    repository_id: int | str,
    run_id: int,
    run_attempt: int,
    trusted_source_sha: str,
    proof_path: str | Path,
    route_policy: Mapping[str, Mapping[str, Any]],
    now: datetime | None = None,
) -> BrokerEvent:
    """Perform one serialized broker decision and emit a provisional proven-event candidate."""
    github = engine.github
    asana = engine.asana
    if asana is None:
        raise BrokerError("live Asana read authority is required for broker mutation admission")
    if FULL_SHA_RE.fullmatch(str(trusted_source_sha).lower()) is None:
        raise BrokerError("trusted broker source must be an exact SHA")
    request_comment = github.get_comment(int(request_comment_id))
    request = parse_request_comment(request_comment)
    pr = github.get_pr(request.pr_number)
    permission = github.collaborator_permission(request.commenter)
    task = asana.get_task(request.task_gid)
    validate_live_request_preconditions(
        request,
        pr=pr,
        task=task,
        permission=permission,
        route_policy=route_policy,
    )

    comments = github.get_comments(request.pr_number)
    verified = authoritative_events_from_comments(
        github,
        comments,
        repository=github.repository,
        repository_id=repository_id,
    )
    current = fold_verified_events(verified)
    if request.action == "takeover":
        validate_takeover_preconditions(request, current=current, route_policy=route_policy)

    current_pr = engine.inspect(pr)
    reviews = github.get_reviews(request.pr_number)
    base = pr.get("base") if isinstance(pr.get("base"), Mapping) else {}
    base_ref = str(base.get("ref") or "main")
    live_main_sha = github.get_ref_sha(f"heads/{base_ref}")
    validate_lifecycle_eligibility(
        request,
        lifecycle=current_pr,
        reviews=reviews,
        live_main_sha=live_main_sha,
        integration_authority=bool(engine.integration_authority),
        current_grant=current,
        route_policy=route_policy,
    )

    issued = now or _now()
    kind, grant_id, generation = decision_for_request(request, current=current, task=task, now=issued)
    action = action_for_state_event(request, current)
    if kind in {"renew", "close"} and current is None:  # defensive; decision already rejects
        raise BrokerError("current grant disappeared during broker decision")
    if kind in {"renew", "close"}:
        branch = current.branch
        starting_head = current.starting_head
        review_id = current.review_id
        main_sha = current.main_sha
        route = current.route
    elif kind == "takeover" and current is not None:
        branch = current.branch
        starting_head = current.starting_head
        review_id = current.review_id
        main_sha = current.main_sha
        route = current.route
    else:
        branch = request.branch
        starting_head = request.head
        review_id = request.review_id
        main_sha = request.main_sha
        route = request.route
    stale_after = issued if kind == "close" else issued + DEFAULT_STALE_AFTER
    payload = make_event_payload(
        repository=github.repository,
        repository_id=repository_id,
        kind=kind,
        request=request,
        grant_id=grant_id,
        generation=generation,
        action=action,
        branch=branch,
        starting_head=starting_head,
        review_id=review_id,
        main_sha=main_sha,
        route=route,
        run_id=run_id,
        run_attempt=run_attempt,
        trusted_source_sha=str(trusted_source_sha).lower(),
        issued_at=issued,
        stale_after=stale_after,
        outcome="closed" if kind == "close" else "accepted",
    )
    provisional = event_from_payload(payload)
    posted = github.add_comment(request.pr_number, provisional_event_comment(provisional))
    try:
        comment_id = int(posted["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BrokerError("GitHub did not return the provisional broker event comment id") from exc
    event = replace(provisional, comment_id=comment_id)
    # Re-read the exact same comment before creating proof so a transport anomaly or
    # server-side mutation cannot be silently bound.
    reread = github.get_comment(comment_id)
    parsed = parse_event_comment(reread)
    if parsed is None or parsed.comment_id != comment_id or parsed.event_digest != event.event_digest:
        raise BrokerError("provisional broker event comment readback mismatch")
    write_proof(proof_path, event)
    return event


def finalize_broker_event(
    *,
    github: Any,
    comment_id: int,
    artifact_id: int,
    artifact_digest: str,
    repository_id: int | str,
    run_id: int,
    run_attempt: int,
) -> BrokerEvent:
    """Write proof transport metadata onto the same provisional event and re-read it."""
    current = github.get_comment(int(comment_id))
    event = parse_event_comment(current)
    if event is None or event.proof_state != "PENDING":
        raise BrokerError("broker-finalize requires the exact provisional broker event comment")
    p = event.payload
    if (
        str(p.get("repository_id")) != str(repository_id)
        or int(p.get("run_id", -1)) != int(run_id)
        or int(p.get("run_attempt", -1)) != int(run_attempt)
        or event.comment_id != int(comment_id)
    ):
        raise BrokerError("broker-finalize run/comment identity mismatch")
    expected_name = artifact_name(run_id, run_attempt, comment_id)
    # The name is deterministic and is intentionally not supplied by the caller.
    if not expected_name:
        raise BrokerError("broker proof artifact name derivation failed")
    body = complete_event_comment(event, artifact_id=int(artifact_id), artifact_digest=artifact_digest)
    github.update_comment(int(comment_id), body)
    reread = github.get_comment(int(comment_id))
    final = parse_event_comment(reread)
    if (
        final is None
        or final.proof_state != "COMPLETE"
        or final.comment_id != int(comment_id)
        or final.event_digest != event.event_digest
        or final.artifact_id != int(artifact_id)
        or final.artifact_digest != artifact_digest.lower()
    ):
        raise BrokerError("final broker event comment proof metadata readback mismatch")
    return final


def current_verified_grant(
    *,
    github: Any,
    pr_number: int,
    repository_id: int | str,
) -> GrantState | None:
    events = authoritative_events_from_comments(
        github,
        github.get_comments(pr_number),
        repository=github.repository,
        repository_id=repository_id,
    )
    return fold_verified_events(events)


def write_github_outputs(path: str | Path, values: Mapping[str, Any]) -> None:
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            text = str(value).lower() if isinstance(value, bool) else str(value)
            if "\n" in text or "\r" in text:
                raise BrokerError(f"GitHub output {key!r} may not contain newlines")
            handle.write(f"{key}={text}\n")
