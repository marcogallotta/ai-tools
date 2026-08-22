#!/usr/bin/env python3
"""Lifecycle V4 event-driven wake activation with zero-turn idle behavior.

Webhook deliveries are hints only. They dirty bounded resource identities, then the
existing lifecycle engine performs the authoritative reread/reconcile. Model turns
are emitted only for new actionable semantic versions and only through a dedicated
Codex app-server thread that is proved idle under a host-side fence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import shlex
import subprocess
import threading
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


STATE_SCHEMA = "dish-pr-lifecycle-v4-state-v1"
WAKE_SCHEMA = "dish-pr-lifecycle-v4-wake-v1"
POLICY_GENERATION = "lifecycle-v4-zero-burn-g2"
RECEIPT_STATES = {"PREPARED", "SUBMITTED", "ACCEPTED", "COMPLETED", "AMBIGUOUS"}
DEFAULT_ACTIVE_OWNERS = frozenset({"Integrator"})
_VOLATILE_KEYS = frozenset({
    "age_seconds",
    "backoff",
    "backoff_seconds",
    "first_seen",
    "generated_at",
    "last_changed",
    "next_retry_seconds",
    "observed_at",
    "retry_after",
    "retry_at",
    "timestamp",
    "updated_at",
})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _safe_semantics(value: Any) -> Any:
    """Remove timing/retry noise while retaining semantic evidence."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered in _VOLATILE_KEYS:
                continue
            if lowered.endswith("_at") and lowered not in {"created_at_authority"}:
                continue
            result[key] = _safe_semantics(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_semantics(item) for item in value]
    return value


def actionable_version(case: Mapping[str, Any], *, policy_generation: str = POLICY_GENERATION) -> str:
    material = {
        "repository": case.get("repository"),
        "reason_class": case.get("reason_class"),
        "pr": case.get("pr"),
        "task": case.get("task"),
        "head": case.get("head"),
        "evidence": _safe_semantics(case.get("evidence") or {}),
        "next_owner": case.get("next_owner"),
        "next_action": case.get("next_action"),
        "wake_policy_generation": policy_generation,
    }
    return _sha(material)


def wake_packet(
    *, owner: str, cases: Sequence[Mapping[str, Any]], versions: Sequence[str], policy_generation: str
) -> dict[str, Any]:
    ordered = sorted(zip(versions, cases), key=lambda pair: pair[0])
    repository_values = sorted({str(case.get("repository") or "") for _, case in ordered if case.get("repository")})
    identity = {
        "owner": owner,
        "versions": [version for version, _ in ordered],
        "wake_policy_generation": policy_generation,
    }
    wake_id = f"wake-{_sha(identity)[:32]}"
    return {
        "schema": WAKE_SCHEMA,
        "wake_id": wake_id,
        "event_type": "NEW_ACTIONABLE_SET",
        "owner": owner,
        "repository": repository_values[0] if len(repository_values) == 1 else repository_values,
        "wake_policy_generation": policy_generation,
        "actionable_versions": [version for version, _ in ordered],
        "cases": [
            {
                "actionable_version": version,
                "case_key": case.get("case_key"),
                "reason_class": case.get("reason_class"),
                "repository": case.get("repository"),
                "pr": case.get("pr"),
                "task": case.get("task"),
                "head": case.get("head"),
                "reviewed_head": case.get("reviewed_head"),
                "review_verdict": case.get("review_verdict"),
                "evidence_fingerprint": case.get("evidence_fingerprint"),
                "evidence": _safe_semantics(case.get("evidence") or {}),
                "next_owner": case.get("next_owner"),
                "next_action": case.get("next_action"),
            }
            for version, case in ordered
        ],
        "instruction": (
            "Re-read live GitHub/Asana and current role authority before acting. "
            "This packet grants no semantic, Review, Integration, merge, or production authority."
        ),
    }


@dataclass(frozen=True)
class DirtySnapshot:
    token: int
    resources: tuple[dict[str, Any], ...]


class V4StateStore:
    """Small durable coalescing/receipt journal; never an authority database."""

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    def _empty(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "next_dirty_token": 0,
            "dirty": {},
            "receipts": {},
            "delivered": {},
            "baseline": None,
            "last_reconcile": None,
        }

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        value = json.loads(self.path.read_text())
        if value.get("schema") != STATE_SCHEMA:
            raise ValueError(f"unexpected lifecycle-v4 state schema: {value.get('schema')!r}")
        return value

    @contextmanager
    def locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read_unlocked()
                yield state
                _atomic_write(self.path, state)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any]:
        with self.locked() as state:
            return json.loads(json.dumps(state))

    def mark_dirty(
        self,
        *,
        provider: str,
        resource_kind: str,
        resource_id: str,
        delivery_id: str | None,
    ) -> int:
        key = f"{provider}:{resource_kind}:{resource_id}"
        with self.locked() as state:
            state["next_dirty_token"] = int(state.get("next_dirty_token") or 0) + 1
            token = int(state["next_dirty_token"])
            previous = dict((state.get("dirty") or {}).get(key) or {})
            deliveries = [str(value) for value in previous.get("delivery_ids") or []]
            if delivery_id and delivery_id not in deliveries:
                deliveries.append(delivery_id)
            deliveries = deliveries[-8:]
            state.setdefault("dirty", {})[key] = {
                "provider": provider,
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "token": token,
                "delivery_ids": deliveries,
                "last_received_at": _utcnow(),
            }
            return token

    def snapshot_dirty(self) -> DirtySnapshot:
        with self.locked() as state:
            resources = tuple(dict(value) for _, value in sorted((state.get("dirty") or {}).items()))
            return DirtySnapshot(token=int(state.get("next_dirty_token") or 0), resources=resources)

    def compare_and_clear(self, snapshot: DirtySnapshot) -> None:
        observed = {
            f"{item['provider']}:{item['resource_kind']}:{item['resource_id']}": int(item["token"])
            for item in snapshot.resources
        }
        with self.locked() as state:
            dirty = state.setdefault("dirty", {})
            for key, token in observed.items():
                current = dirty.get(key)
                if current is not None and int(current.get("token") or -1) == token:
                    del dirty[key]
            state["last_reconcile"] = {
                "finished_at": _utcnow(),
                "snapshot_token": snapshot.token,
            }

    @staticmethod
    def _pending_versions(state: Mapping[str, Any]) -> set[str]:
        result: set[str] = set()
        for receipt in (state.get("receipts") or {}).values():
            if receipt.get("status") in {"PREPARED", "SUBMITTED", "AMBIGUOUS"}:
                result.update(str(value) for value in receipt.get("actionable_versions") or [])
        return result

    def prepare_wakes(
        self,
        cases: Iterable[Mapping[str, Any]],
        *,
        active_owners: frozenset[str] = DEFAULT_ACTIVE_OWNERS,
        policy_generation: str = POLICY_GENERATION,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
        for case in cases:
            owner = str(case.get("next_owner") or "")
            if owner not in active_owners:
                continue
            version = actionable_version(case, policy_generation=policy_generation)
            grouped.setdefault(owner, []).append((version, case))

        prepared: list[dict[str, Any]] = []
        with self.locked() as state:
            pending = self._pending_versions(state)
            delivered = state.setdefault("delivered", {})
            baseline = state.get("baseline") or {}
            baselined = {
                str(version)
                for versions in (baseline.get("actionable_versions") or {}).values()
                for version in versions
            }
            receipts = state.setdefault("receipts", {})
            for owner, members in sorted(grouped.items()):
                owner_delivered = set((delivered.get(owner) or {}).keys())
                new_members = [
                    (version, case)
                    for version, case in members
                    if version not in owner_delivered
                    and version not in pending
                    and version not in baselined
                ]
                if not new_members:
                    continue
                packet = wake_packet(
                    owner=owner,
                    cases=[case for _, case in new_members],
                    versions=[version for version, _ in new_members],
                    policy_generation=policy_generation,
                )
                wake_id = packet["wake_id"]
                receipt = receipts.get(wake_id)
                if receipt is None:
                    receipt = {
                        "wake_id": wake_id,
                        "status": "PREPARED",
                        "owner": owner,
                        "actionable_versions": packet["actionable_versions"],
                        "packet": packet,
                        "start_attempts": 0,
                        "prepared_at": _utcnow(),
                    }
                    receipts[wake_id] = receipt
                prepared.append(dict(receipt))
                pending.update(packet["actionable_versions"])
        return prepared

    def baseline_current(
        self,
        cases: Iterable[Mapping[str, Any]],
        *,
        active_owners: frozenset[str] = DEFAULT_ACTIVE_OWNERS,
        policy_generation: str = POLICY_GENERATION,
    ) -> int:
        """Record the commissioning-time active set without preparing a wake.

        This is a one-shot cold-start operation. Repeating it is an idempotent
        no-op, and invoking it after any wake receipt or delivery fails closed so
        an operator cannot accidentally suppress newly actionable work.
        """
        grouped: dict[str, list[str]] = {}
        for case in cases:
            owner = str(case.get("next_owner") or "")
            if owner in active_owners:
                grouped.setdefault(owner, []).append(
                    actionable_version(case, policy_generation=policy_generation)
                )
        normalized = {
            owner: sorted(set(versions))
            for owner, versions in sorted(grouped.items())
        }
        with self.locked() as state:
            if state.get("baseline") is not None:
                return 0
            if state.get("receipts") or state.get("delivered"):
                raise ValueError("cold-start baseline is unavailable after wake history exists")
            state["baseline"] = {
                "completed_at": _utcnow(),
                "wake_policy_generation": policy_generation,
                "actionable_versions": normalized,
            }
        return sum(len(versions) for versions in normalized.values())

    def pending_receipts(self) -> list[dict[str, Any]]:
        with self.locked() as state:
            return [
                dict(value)
                for _, value in sorted((state.get("receipts") or {}).items())
                if value.get("status") in {"PREPARED", "SUBMITTED", "AMBIGUOUS"}
            ]

    def accepted_receipts(self) -> list[dict[str, Any]]:
        with self.locked() as state:
            return [
                dict(value)
                for _, value in sorted((state.get("receipts") or {}).items())
                if value.get("status") == "ACCEPTED"
            ]

    def mark_submitted(self, wake_id: str) -> dict[str, Any]:
        with self.locked() as state:
            receipt = state.setdefault("receipts", {}).get(wake_id)
            if receipt is None:
                raise KeyError(wake_id)
            if receipt.get("status") != "PREPARED":
                raise ValueError(f"wake {wake_id} is not PREPARED")
            receipt["status"] = "SUBMITTED"
            receipt["start_attempts"] = int(receipt.get("start_attempts") or 0) + 1
            receipt["submitted_at"] = _utcnow()
            return dict(receipt)

    def mark_accepted(self, wake_id: str, *, turn_id: str | None = None) -> dict[str, Any]:
        with self.locked() as state:
            receipt = state.setdefault("receipts", {}).get(wake_id)
            if receipt is None:
                raise KeyError(wake_id)
            if receipt.get("status") not in {"SUBMITTED", "AMBIGUOUS"}:
                raise ValueError(f"wake {wake_id} cannot be accepted from {receipt.get('status')}")
            receipt["status"] = "ACCEPTED"
            receipt["accepted_at"] = _utcnow()
            if turn_id:
                receipt["turn_id"] = turn_id
            owner = str(receipt["owner"])
            owner_delivered = state.setdefault("delivered", {}).setdefault(owner, {})
            for version in receipt.get("actionable_versions") or []:
                owner_delivered[str(version)] = {
                    "wake_id": wake_id,
                    "accepted_at": receipt["accepted_at"],
                }
            return dict(receipt)

    def mark_completed(self, wake_id: str) -> dict[str, Any]:
        with self.locked() as state:
            receipt = state.setdefault("receipts", {}).get(wake_id)
            if receipt is None:
                raise KeyError(wake_id)
            if receipt.get("status") != "ACCEPTED":
                raise ValueError(f"wake {wake_id} cannot complete from {receipt.get('status')}")
            receipt["status"] = "COMPLETED"
            receipt["completed_at"] = _utcnow()
            return dict(receipt)

    def mark_ambiguous(self, wake_id: str, *, error_type: str) -> dict[str, Any]:
        with self.locked() as state:
            receipt = state.setdefault("receipts", {}).get(wake_id)
            if receipt is None:
                raise KeyError(wake_id)
            if receipt.get("status") != "SUBMITTED":
                raise ValueError(f"wake {wake_id} cannot become ambiguous from {receipt.get('status')}")
            receipt["status"] = "AMBIGUOUS"
            receipt["ambiguous_at"] = _utcnow()
            receipt["error_type"] = error_type
            return dict(receipt)

    def prove_not_accepted(self, wake_id: str, *, proof: str) -> dict[str, Any]:
        """Permit a retry only after history mechanically proves non-acceptance."""
        with self.locked() as state:
            receipt = state.setdefault("receipts", {}).get(wake_id)
            if receipt is None:
                raise KeyError(wake_id)
            if receipt.get("status") not in {"SUBMITTED", "AMBIGUOUS"}:
                raise ValueError(f"wake {wake_id} is not an uncertain submission")
            receipt["status"] = "PREPARED"
            receipt["non_acceptance_proof"] = proof
            receipt["non_acceptance_proved_at"] = _utcnow()
            return dict(receipt)


class AppServerProtocol(Protocol):
    def thread_read(self, thread_id: str, *, include_turns: bool) -> Mapping[str, Any]: ...
    def thread_resume(self, thread_id: str) -> Mapping[str, Any]: ...
    def turn_start(self, thread_id: str, packet: Mapping[str, Any], *, client_user_message_id: str) -> Mapping[str, Any]: ...


class CodexAppServer:
    """Minimal persistent Codex app-server v2 JSONL client."""

    def __init__(self, command: Sequence[str] | str, *, thread_id: str | None = None):
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            raise ValueError("Codex app-server command is empty")
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("Codex app-server stdio unavailable")
        self._next_id = 0
        self._lock = threading.Lock()
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "dish-pr-lifecycle-v4",
                    "title": "Dish Lifecycle V4",
                    "version": "1",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [
                        "item/agentMessage/delta",
                        "item/reasoning/summaryTextDelta",
                        "thread/tokenUsage/updated",
                    ],
                },
            },
        )
        self._notify("initialized", {})
        # A stored-but-unloaded lifecycle thread reports `thread/read` status
        # `notLoaded`; explicitly resume it in this same process before any
        # wake admission so the persistent thread identity is preserved.
        if thread_id:
            self.thread_resume(thread_id)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()

    def _write(self, value: Mapping[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(_canonical(value) + "\n")
        self.process.stdin.flush()

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        with self._lock:
            self._write({"method": method, "params": dict(params)})

    def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._write({"method": method, "id": request_id, "params": dict(params)})
            assert self.process.stdout is not None
            while True:
                line = self.process.stdout.readline()
                if line == "":
                    raise RuntimeError(f"Codex app-server exited during {method}")
                message = json.loads(line)
                if message.get("id") != request_id:
                    # Notifications and unrelated server messages are intentionally ignored;
                    # this bridge starts turns but never supplies semantic tool responses.
                    continue
                if "error" in message:
                    error = message.get("error") or {}
                    raise RuntimeError(f"Codex app-server {method} failed: {error.get('code')} {error.get('message')}")
                result = message.get("result")
                if not isinstance(result, Mapping):
                    raise RuntimeError(f"Codex app-server {method} returned invalid result")
                return dict(result)

    def thread_read(self, thread_id: str, *, include_turns: bool) -> Mapping[str, Any]:
        return self._request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})

    def thread_resume(self, thread_id: str) -> Mapping[str, Any]:
        return self._request("thread/resume", {"threadId": thread_id})

    def turn_start(
        self,
        thread_id: str,
        packet: Mapping[str, Any],
        *,
        client_user_message_id: str,
    ) -> Mapping[str, Any]:
        text = (
            "Dish lifecycle V4 actionable wake. Treat this packet as an event hint only; re-read live authority before action.\n"
            + json.dumps(packet, sort_keys=True)
        )
        return self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "clientUserMessageId": client_user_message_id,
                "input": [{"type": "text", "text": text}],
            },
        )


def _status_type(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("type") or "")
    return str(value or "")


def _thread_status(read: Mapping[str, Any]) -> str:
    thread = read.get("thread") if isinstance(read.get("thread"), Mapping) else {}
    status = thread.get("status") if isinstance(thread, Mapping) else None
    return _status_type(status)


def _history_contains_wake(read: Mapping[str, Any], wake_id: str) -> bool:
    thread = read.get("thread") if isinstance(read.get("thread"), Mapping) else {}
    for turn in thread.get("turns") or []:
        if not isinstance(turn, Mapping):
            continue
        for item in turn.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("type") or "") in {"userMessage", "user_message"} and str(item.get("clientId") or "") == wake_id:
                return True
    return False


def _turn_status_for_wake(read: Mapping[str, Any], wake_id: str) -> str | None:
    """Mechanically reconstruct terminal turn state from persisted thread history."""
    thread = read.get("thread") if isinstance(read.get("thread"), Mapping) else {}
    for turn in thread.get("turns") or []:
        if not isinstance(turn, Mapping):
            continue
        for item in turn.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("type") or "") in {"userMessage", "user_message"} and str(item.get("clientId") or "") == wake_id:
                return _status_type(turn.get("status"))
    return None


class WakeBridge:
    def __init__(self, *, store: V4StateStore, app_server: AppServerProtocol, thread_id: str, fence_path: Path):
        self.store = store
        self.app_server = app_server
        self.thread_id = thread_id
        self.fence_path = fence_path

    @contextmanager
    def _fence(self):
        self.fence_path.parent.mkdir(parents=True, exist_ok=True)
        with self.fence_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def reconcile_uncertain(self, receipt: Mapping[str, Any]) -> str:
        wake_id = str(receipt["wake_id"])
        read = self.app_server.thread_read(self.thread_id, include_turns=True)
        if _history_contains_wake(read, wake_id):
            self.store.mark_accepted(wake_id)
            return "accepted-from-history"
        status = _thread_status(read)
        if status != "idle":
            # Active/unknown/not-loaded thread state cannot mechanically prove the
            # original start was not accepted. The receipt must stay AMBIGUOUS with
            # zero replay until a later idle read can be checked.
            return "still-ambiguous"
        # A complete thread/read with no matching client id, while the thread is
        # provably idle, is the mechanical proof required before a retry.
        self.store.prove_not_accepted(wake_id, proof="thread/read includeTurns=true status=idle contains no matching clientId")
        return "not-accepted-proved"

    def reconcile_accepted(self, receipt: Mapping[str, Any]) -> str:
        wake_id = str(receipt["wake_id"])
        read = self.app_server.thread_read(self.thread_id, include_turns=True)
        status = _turn_status_for_wake(read, wake_id)
        if status == "completed":
            self.store.mark_completed(wake_id)
            return "completed"
        return "in-flight"

    def dispatch_pending(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with self._fence():
            for receipt in self.store.accepted_receipts():
                outcome = self.reconcile_accepted(receipt)
                if outcome == "completed":
                    results.append({"wake_id": str(receipt["wake_id"]), "result": "completed"})
            for receipt in self.store.pending_receipts():
                wake_id = str(receipt["wake_id"])
                status = str(receipt.get("status") or "")
                if status in {"SUBMITTED", "AMBIGUOUS"}:
                    recovery = self.reconcile_uncertain(receipt)
                    results.append({"wake_id": wake_id, "result": recovery})
                    if recovery != "not-accepted-proved":
                        continue
                    receipt = next(value for value in self.store.pending_receipts() if value["wake_id"] == wake_id)
                    status = str(receipt.get("status") or "")
                if status != "PREPARED":
                    continue
                read = self.app_server.thread_read(self.thread_id, include_turns=False)
                status_type = _thread_status(read)
                if status_type == "notLoaded":
                    # A stored-but-unloaded dedicated thread must be explicitly
                    # resumed in this process before it can prove idle.
                    self.app_server.thread_resume(self.thread_id)
                    read = self.app_server.thread_read(self.thread_id, include_turns=False)
                    status_type = _thread_status(read)
                if status_type != "idle":
                    results.append({"wake_id": wake_id, "result": "thread-not-idle"})
                    continue
                packet = receipt["packet"]
                self.store.mark_submitted(wake_id)
                try:
                    response = self.app_server.turn_start(
                        self.thread_id,
                        packet,
                        client_user_message_id=wake_id,
                    )
                except Exception as exc:
                    self.store.mark_ambiguous(wake_id, error_type=type(exc).__name__)
                    results.append({"wake_id": wake_id, "result": "acceptance-ambiguous", "error_type": type(exc).__name__})
                    continue
                turn = response.get("turn") if isinstance(response.get("turn"), Mapping) else {}
                self.store.mark_accepted(wake_id, turn_id=str(turn.get("id") or "") or None)
                results.append({"wake_id": wake_id, "result": "accepted", "turn_id": turn.get("id")})
        return results


def verify_github_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)


def verify_asana_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)


def github_dirty_resources(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    repository = payload.get("repository") if isinstance(payload.get("repository"), Mapping) else {}
    repo = str(repository.get("full_name") or "")
    if not repo:
        return []
    resources: set[tuple[str, str]] = {("repository", repo)}
    pull = payload.get("pull_request") if isinstance(payload.get("pull_request"), Mapping) else None
    if pull and pull.get("number") is not None:
        resources.add(("pull_request", f"{repo}#{int(pull['number'])}"))
    workflow = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), Mapping) else None
    if workflow and workflow.get("pull_requests"):
        for item in workflow.get("pull_requests") or []:
            if isinstance(item, Mapping) and item.get("number") is not None:
                resources.add(("pull_request", f"{repo}#{int(item['number'])}"))
    return sorted(resources)


def asana_dirty_resources(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    resources: set[tuple[str, str]] = set()
    for event in payload.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        resource = event.get("resource") if isinstance(event.get("resource"), Mapping) else {}
        gid = str(resource.get("gid") or "")
        kind = str(resource.get("resource_type") or "resource")
        if gid:
            resources.add((kind, gid))
        parent = event.get("parent") if isinstance(event.get("parent"), Mapping) else {}
        parent_gid = str(parent.get("gid") or "")
        if parent_gid:
            resources.add((str(parent.get("resource_type") or "resource"), parent_gid))
    return sorted(resources)


def ingest_event(
    store: V4StateStore,
    *,
    provider: str,
    payload: Mapping[str, Any],
    delivery_id: str | None,
) -> int:
    resolver = github_dirty_resources if provider == "github" else asana_dirty_resources
    count = 0
    for resource_kind, resource_id in resolver(payload):
        store.mark_dirty(
            provider=provider,
            resource_kind=resource_kind,
            resource_id=resource_id,
            delivery_id=delivery_id,
        )
        count += 1
    return count


class V4Reconciler:
    """Coalesce dirty hints around one existing authoritative lifecycle projection reread."""

    def __init__(
        self,
        *,
        store: V4StateStore,
        authoritative_cases: Callable[[], Iterable[Mapping[str, Any]]],
        bridge: WakeBridge | None,
        active_owners: frozenset[str] = DEFAULT_ACTIVE_OWNERS,
    ):
        self.store = store
        self.authoritative_cases = authoritative_cases
        self.bridge = bridge
        self.active_owners = active_owners

    def baseline_current(self) -> dict[str, Any]:
        """Baseline current authoritative cases with a hard zero-turn result."""
        cases = [dict(value) for value in self.authoritative_cases()]
        baselined = self.store.baseline_current(cases, active_owners=self.active_owners)
        return {
            "actionable_cases": len(cases),
            "baselined": baselined,
            "prepared": 0,
            "wake_results": [],
            "model_turns_started": 0,
        }

    def reconcile(self, *, force: bool = False) -> dict[str, Any]:
        snapshot = self.store.snapshot_dirty()
        if not snapshot.resources and not force:
            # Heartbeat/no-change performs no authoritative reread. A durable
            # receipt from an earlier interrupted dispatch may still need its
            # fenced recovery pass; without one it could remain stranded until
            # an unrelated webhook arrives.
            wake_results = self.bridge.dispatch_pending() if self.bridge is not None else []
            started = sum(1 for value in wake_results if value.get("result") == "accepted")
            return {
                "dirty": 0,
                "prepared": 0,
                "wake_results": wake_results,
                "model_turns_started": started,
            }
        cases = [dict(value) for value in self.authoritative_cases()]
        prepared = self.store.prepare_wakes(cases, active_owners=self.active_owners)
        self.store.compare_and_clear(snapshot)
        wake_results = self.bridge.dispatch_pending() if self.bridge is not None else []
        started = sum(1 for value in wake_results if value.get("result") == "accepted")
        return {
            "dirty": len(snapshot.resources),
            "actionable_cases": len(cases),
            "prepared": len(prepared),
            "wake_results": wake_results,
            "model_turns_started": started,
        }
