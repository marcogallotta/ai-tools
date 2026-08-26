#!/usr/bin/env python3
"""Disposable local multi-agent conversation/forking prototype."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = Path(os.environ.get("AGENT_FORK_DATA", ROOT / "data"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ident(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Store:
    """Append-only JSONL store replayed into a small in-memory projection."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.branches: dict[str, dict[str, Any]] = {}
        self.generations: dict[str, int] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        self._replay()

    def _replay(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                self._apply(json.loads(line))
        # A process died while an adapter was running: recover as stopped.
        for branch in self.branches.values():
            branch["status"] = "stopped"

    def _append(self, event: dict[str, Any]) -> None:
        event = {"at": now(), **event}
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._apply(event)

    def _apply(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind in {"branch_created", "branch_forked"}:
            branch = event["branch"]
            self.branches[branch["id"]] = branch
            self.generations.setdefault(branch["id"], 0)
        elif kind == "message_added":
            self.branches[event["branch_id"]]["messages"].append(event["message"])
        elif kind == "status_changed":
            branch_id = event["branch_id"]
            self.branches[branch_id]["status"] = event["status"]
            self.generations[branch_id] = event["generation"]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            branches = json.loads(json.dumps(list(self.branches.values())))
        branches.sort(key=lambda branch: branch["created_at"])
        return {"branches": branches}

    def create(self, name: str, prompt: str = "") -> dict[str, Any]:
        branch = {
            "id": ident("branch"),
            "name": name.strip() or "Agent",
            "prompt": prompt.strip(),
            "parent_id": None,
            "fork_message_id": None,
            "created_at": now(),
            "status": "stopped",
            "messages": [],
        }
        self._append({"type": "branch_created", "branch": branch})
        return branch

    def fork(self, branch_id: str, message_id: str, name: str) -> dict[str, Any]:
        with self.lock:
            parent = self._branch(branch_id)
            positions = [i for i, msg in enumerate(parent["messages"]) if msg["id"] == message_id]
            if not positions:
                raise ValueError("Fork message not found")
            history = json.loads(json.dumps(parent["messages"][: positions[0] + 1]))
            branch = {
                "id": ident("branch"),
                "name": name.strip() or f"Fork of {parent['name']}",
                "prompt": parent["prompt"],
                "parent_id": branch_id,
                "fork_message_id": message_id,
                "created_at": now(),
                "status": "stopped",
                "messages": history,
            }
        self._append({"type": "branch_forked", "branch": branch})
        return branch

    def add_message(self, branch_id: str, role: str, text: str) -> dict[str, Any]:
        message = {"id": ident("msg"), "role": role, "text": text.strip(), "created_at": now()}
        if not message["text"]:
            raise ValueError("Message cannot be empty")
        self._append({"type": "message_added", "branch_id": branch_id, "message": message})
        return message

    def status(self, branch_id: str, status: str) -> int:
        with self.lock:
            self._branch(branch_id)
            generation = self.generations.get(branch_id, 0) + 1
        self._append({
            "type": "status_changed", "branch_id": branch_id,
            "status": status, "generation": generation,
        })
        return generation

    def history(self, branch_id: str) -> tuple[str, list[dict[str, Any]], int]:
        with self.lock:
            branch = self._branch(branch_id)
            return (
                branch["prompt"],
                json.loads(json.dumps(branch["messages"])),
                self.generations.get(branch_id, 0),
            )

    def finish(self, branch_id: str, generation: int, output: str) -> bool:
        with self.lock:
            if self.generations.get(branch_id) != generation:
                return False
            # Hold the re-entrant lock so stop/redirect cannot land between
            # the generation check and the assistant output append.
            self.add_message(branch_id, "assistant", output)
            self._append({
                "type": "status_changed", "branch_id": branch_id,
                "status": "stopped", "generation": generation,
            })
            return True

    def compare(self, branch_ids: list[str]) -> list[dict[str, str | None]]:
        result = []
        with self.lock:
            for branch_id in branch_ids:
                branch = self._branch(branch_id)
                latest = next(
                    (msg["text"] for msg in reversed(branch["messages"]) if msg["role"] == "assistant"),
                    None,
                )
                result.append({"id": branch_id, "name": branch["name"], "output": latest})
        return result

    def _branch(self, branch_id: str) -> dict[str, Any]:
        try:
            return self.branches[branch_id]
        except KeyError as exc:
            raise ValueError("Branch not found") from exc


class Adapter:
    """One replaceable adapter: command from env, otherwise deterministic stub."""

    def __init__(self, command: str | None = None, delay: float = 0.35):
        self.command = shlex.split(command) if command else None
        self.delay = delay

    def run(self, prompt: str, messages: list[dict[str, Any]]) -> str:
        payload = json.dumps({"prompt": prompt, "messages": messages})
        if self.command:
            completed = subprocess.run(
                self.command, input=payload, text=True, capture_output=True, timeout=120, check=False,
            )
            if completed.returncode:
                raise RuntimeError(completed.stderr.strip() or f"adapter exited {completed.returncode}")
            return completed.stdout.strip()
        time.sleep(self.delay)
        last = next((msg["text"] for msg in reversed(messages) if msg["role"] == "user"), "")
        return f"Stub agent ({len(messages)} messages): {last}"


class Prototype:
    def __init__(self, store: Store, adapter: Adapter):
        self.store = store
        self.adapter = adapter

    def send(self, branch_id: str, text: str, redirect: bool = False) -> dict[str, Any]:
        if redirect:
            self.store.status(branch_id, "stopped")  # invalidates an in-flight reply
        message = self.store.add_message(branch_id, "user", text)
        generation = self.store.status(branch_id, "running")
        prompt, history, _ = self.store.history(branch_id)
        threading.Thread(
            target=self._invoke, args=(branch_id, generation, prompt, history), daemon=True,
        ).start()
        return message

    def _invoke(self, branch_id: str, generation: int, prompt: str, history: list[dict[str, Any]]) -> None:
        try:
            output = self.adapter.run(prompt, history)
        except Exception as exc:  # disposable UI should surface adapter failures
            output = f"Adapter error: {exc}"
        # A discarded generation may still complete at the adapter boundary;
        # Store.finish ignores its result rather than terminating the process.
        self.store.finish(branch_id, generation, output)


class Handler(BaseHTTPRequestHandler):
    prototype: Prototype

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(self.prototype.store.snapshot())
        elif path in {"/", "/index.html"}:
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self._body()
            parts = [part for part in path.split("/") if part]
            if parts == ["api", "branches"]:
                result = self.prototype.store.create(data.get("name", ""), data.get("prompt", ""))
            elif len(parts) == 4 and parts[:2] == ["api", "branches"]:
                branch_id, action = parts[2], parts[3]
                if action == "messages":
                    result = self.prototype.send(branch_id, data.get("text", ""))
                elif action == "redirect":
                    result = self.prototype.send(branch_id, data.get("text", ""), redirect=True)
                elif action == "stop":
                    result = {"generation": self.prototype.store.status(branch_id, "stopped")}
                elif action == "fork":
                    result = self.prototype.store.fork(branch_id, data["message_id"], data.get("name", ""))
                else:
                    raise ValueError("Unknown action")
            elif parts == ["api", "compare"]:
                result = self.prototype.store.compare(data.get("branch_ids", []))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(result, HTTPStatus.CREATED)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    store = Store(DATA / "events.jsonl")
    Handler.prototype = Prototype(store, Adapter(os.environ.get("FORK_AGENT_COMMAND")))
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8765"))), Handler)
    print(f"Agent forks: http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
