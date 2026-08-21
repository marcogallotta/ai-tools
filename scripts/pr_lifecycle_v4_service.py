#!/usr/bin/env python3
"""Repository-owned Lifecycle V4 webhook receiver and Integrator wake service."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from typing import Any, Mapping


REPO = Path(os.getenv("DISH_LIFECYCLE_V4_REPO") or Path(__file__).resolve().parents[1]).resolve()
STATE_DIR = Path(os.getenv("DISH_LIFECYCLE_V4_STATE_DIR") or Path.home() / ".local/state/dish/pr-lifecycle-v4")
PROJECTION = Path(
    os.getenv("DISH_LIFECYCLE_V4_PROJECTION")
    or Path.home() / ".local/state/dish/pr-lifecycle/lifecycle.json"
)
CODEX = os.getenv("DISH_LIFECYCLE_V4_CODEX") or str(
    Path.home() / ".codex/packages/standalone/current/codex"
)
PYTHON = os.getenv("DISH_LIFECYCLE_V4_PYTHON") or str(REPO / "dish/.venv/bin/python")
THREAD_FILE = STATE_DIR / "thread.json"
GITHUB_SECRET_FILE = STATE_DIR / "github-webhook-secret"
ASANA_SECRET_FILE = STATE_DIR / "asana-webhook-secret"

sys.path.insert(0, str(REPO / "scripts"))
from pr_lifecycle_projection import read_projection  # noqa: E402
from codex_app_server_daemon import CodexDaemonAppServer  # noqa: E402
from pr_lifecycle_v4 import (  # noqa: E402
    V4Reconciler,
    V4StateStore,
    WakeBridge,
    ingest_event,
    verify_asana_signature,
    verify_github_signature,
)


def log(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, "at": time.time(), **values}, sort_keys=True), flush=True)


def ensure_secret(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


def app_server_socket() -> Path:
    return Path(
        os.getenv("DISH_LIFECYCLE_V4_APP_SERVER_SOCKET")
        or Path.home() / ".codex/app-server-control/app-server-control.sock"
    )


def thread_params() -> dict[str, Any]:
    return {
        "cwd": str(REPO),
        "ephemeral": False,
        "serviceName": "Dish Lifecycle V4 Integrator",
        "developerInstructions": (
            "You are the dedicated Dish Lifecycle V4 Integrator diagnosis thread. "
            "For every wake, re-read dish/docs/agents/index.md and integration.md plus "
            "live GitHub/Asana authority. This commissioned phase is diagnosis-only: "
            "never write GitHub or Asana, never dispatch another agent, never run "
            "watch --dispatch, never invoke Integration/merge/local-launcher authority, "
            "and never perform semantic Implementation. Work only on the exact "
            "Integrator-owned cases in the wake packet, then become idle."
        ),
    }


def start_thread(client: CodexDaemonAppServer) -> str:
    response = client._request("thread/start", thread_params())
    thread = response.get("thread") if isinstance(response.get("thread"), Mapping) else {}
    thread_id = str(thread.get("id") or "")
    if not thread_id:
        raise RuntimeError("thread/start returned no thread id")
    tmp = THREAD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"thread_id": thread_id}, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, THREAD_FILE)
    return thread_id


def create_thread() -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    ensure_secret(GITHUB_SECRET_FILE)
    client = CodexDaemonAppServer(app_server_socket())
    try:
        return start_thread(client)
    finally:
        client.close()


class Runtime:
    def __init__(self) -> None:
        state_path = Path(os.getenv("DISH_LIFECYCLE_V4_STATE_PATH") or STATE_DIR / "state.json")
        self.store = V4StateStore(state_path)
        self.github_secret = ensure_secret(GITHUB_SECRET_FILE)
        self.app_server = CodexDaemonAppServer(app_server_socket())
        stored_thread = ""
        if THREAD_FILE.exists():
            stored_thread = str(json.loads(THREAD_FILE.read_text(encoding="utf-8")).get("thread_id") or "")
        if stored_thread:
            try:
                self.app_server.thread_resume(stored_thread)
                self.thread_id = stored_thread
            except RuntimeError as exc:
                if "no rollout found for thread id" not in str(exc):
                    raise
                self.thread_id = start_thread(self.app_server)
                log("replaced_unpersisted_thread")
        else:
            self.thread_id = start_thread(self.app_server)
        self.bridge = WakeBridge(
            store=self.store,
            app_server=self.app_server,
            thread_id=self.thread_id,
            fence_path=STATE_DIR / "integrator.fence",
        )
        wake_enabled = os.getenv("DISH_LIFECYCLE_V4_WAKE_ENABLED") == "1"
        self.reconciler = V4Reconciler(
            store=self.store,
            authoritative_cases=self.authoritative_cases,
            bridge=self.bridge if wake_enabled else None,
            active_owners=frozenset({"Integrator"}) if wake_enabled else frozenset(),
        )
        self.pending = threading.Event()
        self.stop = threading.Event()
        self.reconcile_lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        self.metrics = {
            "accepted_events": 0,
            "reconciles": 0,
            "model_turns_started": 0,
            "heartbeats": 0,
            "signature_rejections": 0,
            "wake_enabled": int(wake_enabled),
        }

    def baseline_current(self) -> dict[str, Any]:
        with self.reconcile_lock:
            cases = self.authoritative_cases()
            baselined = self.store.baseline_current(cases, active_owners=frozenset({"Integrator"}))
        result = {"actionable_cases": len(cases), "baselined": baselined, "model_turns_started": 0}
        log("baseline", **result)
        return result

    def authoritative_cases(self) -> list[dict[str, Any]]:
        result = subprocess.run(
            [
                PYTHON,
                str(REPO / "scripts/pr_lifecycle.py"),
                "--repo",
                "marcogallotta/ai-tools",
                "--http-timeout",
                "10",
                "--projection-path",
                str(PROJECTION),
                "status",
                "--format",
                "json",
            ],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
            timeout=1200,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {detail[-1000:]}" if detail else ""
            raise RuntimeError(
                f"authoritative lifecycle reread failed rc={result.returncode}{suffix}"
            )
        projection = read_projection(PROJECTION)
        integrator = projection.get("v3", {}).get("integrator", {})
        cases = integrator.get("active_cases", []) if isinstance(integrator, Mapping) else []
        return [dict(case) for case in cases if isinstance(case, Mapping) and case.get("next_owner") == "Integrator"]

    def record(self, **increments: int) -> None:
        with self.metrics_lock:
            for key, value in increments.items():
                self.metrics[key] = int(self.metrics.get(key, 0)) + value

    def reconcile(self, *, force: bool = False) -> dict[str, Any]:
        with self.reconcile_lock:
            result = self.reconciler.reconcile(force=force)
        self.record(reconciles=1, model_turns_started=int(result.get("model_turns_started") or 0))
        log("reconcile", **result)
        return result

    def worker(self) -> None:
        while not self.stop.is_set():
            self.pending.wait(1.0)
            if not self.pending.is_set():
                continue
            self.pending.clear()
            try:
                self.reconcile()
            except Exception as exc:
                log("reconcile_failed", error_type=type(exc).__name__, error=str(exc))
                self.pending.set()
                self.stop.wait(30)

    def health(self) -> dict[str, Any]:
        thread = self.app_server.thread_read(self.thread_id, include_turns=False).get("thread", {})
        with self.metrics_lock:
            metrics = dict(self.metrics)
        return {
            "status": "ok",
            "schema": "dish-pr-lifecycle-v4-health-v1",
            "thread_id": self.thread_id,
            "thread_status": thread.get("status") if isinstance(thread, Mapping) else "unknown",
            "dirty": len(self.store.snapshot_dirty().resources),
            "metrics": metrics,
            "source_root": str(REPO),
            "state_path": str(self.store.path),
        }


class Handler(BaseHTTPRequestHandler):
    runtime: Runtime
    server_version = "DishLifecycleV4/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log("http", client=self.client_address[0], message=fmt % args)

    def send_json(self, code: int, value: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> None:
        body = json.dumps(dict(value), sort_keys=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/healthz":
            try:
                self.send_json(200, self.runtime.health())
            except Exception as exc:
                self.send_json(503, {"status": "error", "error_type": type(exc).__name__})
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1_048_576:
            self.send_json(413, {"error": "payload_too_large"})
            return
        body = self.rfile.read(length)
        if self.path.rstrip("/") == "/webhooks/asana" and self.headers.get("X-Hook-Secret"):
            secret = str(self.headers["X-Hook-Secret"])
            if ASANA_SECRET_FILE.exists():
                existing = ASANA_SECRET_FILE.read_text(encoding="utf-8").strip()
                if existing != secret:
                    self.send_json(409, {"error": "handshake_secret_conflict"})
                    return
            else:
                fd = os.open(ASANA_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(secret + "\n")
            self.send_json(200, {"status": "handshake"}, {"X-Hook-Secret": secret})
            log("asana_handshake")
            return
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid_json"})
            return
        if not isinstance(payload, Mapping):
            self.send_json(400, {"error": "invalid_payload"})
            return
        if self.path.rstrip("/") == "/webhooks/github":
            valid = verify_github_signature(body, self.headers.get("X-Hub-Signature-256"), self.runtime.github_secret)
            provider = "github"
            delivery_id = self.headers.get("X-GitHub-Delivery")
        elif self.path.rstrip("/") == "/webhooks/asana":
            if not ASANA_SECRET_FILE.exists():
                self.send_json(503, {"error": "asana_handshake_incomplete"})
                return
            valid = verify_asana_signature(
                body,
                self.headers.get("X-Hook-Signature"),
                ASANA_SECRET_FILE.read_text(encoding="utf-8").strip(),
            )
            provider = "asana"
            delivery_id = self.headers.get("X-Request-Id")
        else:
            self.send_json(404, {"error": "not_found"})
            return
        if not valid:
            self.runtime.record(signature_rejections=1)
            self.send_json(401, {"error": "invalid_signature"})
            return
        count = ingest_event(self.runtime.store, provider=provider, payload=payload, delivery_id=delivery_id)
        if count:
            self.runtime.record(accepted_events=1)
            self.runtime.pending.set()
        elif provider == "asana":
            self.runtime.record(heartbeats=1)
        self.send_json(202 if count else 200, {"accepted": True, "dirty_resources": count})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--create-thread", action="store_true")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8797)
    args = parser.parse_args()
    if args.create_thread:
        create_thread()
        return 0
    runtime = Runtime()
    Handler.runtime = runtime
    worker = threading.Thread(target=runtime.worker, name="lifecycle-v4-reconcile", daemon=True)
    worker.start()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    log("started", bind=args.bind, port=args.port)

    def startup_reconcile() -> None:
        try:
            if os.getenv("DISH_LIFECYCLE_V4_BASELINE_ON_START") == "1":
                if runtime.store.read().get("baseline") is None:
                    runtime.baseline_current()
                else:
                    log("baseline_existing")
            runtime.reconcile(force=True)
        except Exception as exc:
            log("startup_reconcile_failed", error_type=type(exc).__name__, error=str(exc))

    threading.Thread(
        target=startup_reconcile,
        name="lifecycle-v4-startup-reconcile",
        daemon=True,
    ).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        runtime.stop.set()
        server.server_close()
        runtime.app_server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
