"""Durable local-only Integration handoff and single-owner execution fence."""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from typing import Any, Mapping
import uuid

from pr_lifecycle_support import FULL_SHA_RE, LifecycleError


HANDOFF_MARKER = "dish-local-integration-handoff:v1"
CLAIM_SCHEMA = "dish-local-integration-claim-v1"
HANDOFF_SCHEMA = "dish-pr-local-integration-v1"
ATTEMPT_RESULT_SCHEMA = "dish-integration-attempt-result-v1"
MAX_INTEGRATION_ATTEMPTS = 3
ATTEMPT_RETENTION = 5
_PHASES = {
    "claimed",
    "certifying",
    "reconciling",
    "reconciled",
    "premerge",
    "merged",
    "stopped-semantic",
    "failed-evidence",
    "returned",
    "head-changed",
}
_MARKER_RE = re.compile(r"<!--\s*dish-local-integration-handoff:v1\s+(?P<fields>.*?)\s*-->", re.I | re.S)
_TRANSIENT_TOKENS = (
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "rate limit",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "temporary failure",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_repository(repository: str) -> str:
    value = repository.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", value):
        raise LifecycleError(f"invalid repository identity for local Integration claim: {repository!r}")
    return value.replace("/", "--")


def state_root() -> Path:
    configured = os.getenv("DISH_LOCAL_INTEGRATION_STATE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home().resolve() / ".local" / "state" / "dish" / "integration"


def _ensure_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_root(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise LifecycleError(f"local Integration claim state must not be a symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"cannot read local Integration claim {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"local Integration claim must be a JSON object: {path}")
    return value


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_held(path: Path) -> bool:
    _ensure_root(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "r+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _tail(path: Path, limit: int = 32768) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def transient_infrastructure_reason(stdout_path: str | None, stderr_path: str | None) -> str | None:
    text = "\n".join(
        _tail(Path(value).expanduser().resolve())
        for value in (stderr_path, stdout_path)
        if value
    ).lower()
    return next((token for token in _TRANSIENT_TOKENS if token in text), None)


def handoff_key(*, repository: str, pr_number: int, branch: str, head: str, review_id: int, main_sha: str) -> str:
    raw = f"{repository}|{pr_number}|{branch}|{head}|{review_id}|{main_sha}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def marker(*, head: str, key: str, main_sha: str, review_id: int) -> str:
    return f"<!-- {HANDOFF_MARKER} head={head} key={key} main={main_sha} review={review_id} -->"


def _parse_marker(body: str) -> dict[str, str] | None:
    match = _MARKER_RE.search(body or "")
    if not match:
        return None
    fields: dict[str, str] = {}
    for token in match.group("fields").split():
        if "=" not in token:
            return None
        key, value = token.split("=", 1)
        if not key or key in fields:
            return None
        fields[key] = value
    required = {"head", "key", "main", "review"}
    if not required <= fields.keys():
        return None
    if FULL_SHA_RE.fullmatch(fields["head"]) is None or FULL_SHA_RE.fullmatch(fields["main"]) is None:
        return None
    if not fields["review"].isdigit():
        return None
    return fields


def find_handoff(
    comments: list[dict[str, Any]], *, head: str, key: str, main_sha: str, review_id: int
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for comment in comments:
        fields = _parse_marker(str(comment.get("body") or ""))
        if fields is None:
            continue
        if (
            fields["head"] == head
            and fields["key"] == key
            and fields["main"] == main_sha
            and int(fields["review"]) == review_id
        ):
            matches.append(comment)
    if not matches:
        return None
    matches.sort(key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)))
    return matches[-1]


def load_attempt_result(state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    raw_path = state.get("attempt_result_path")
    if not raw_path:
        return None
    path = Path(str(raw_path)).expanduser().resolve()
    if path.parent != state_root() or path.suffix != ".json":
        raise LifecycleError("Integration attempt result path is outside the repository-owned state root")
    result = _read(path)
    if result is None or result.get("schema") != ATTEMPT_RESULT_SCHEMA:
        return None
    if result.get("claim_id") != state.get("claim_id"):
        raise LifecycleError("Integration attempt result does not match its durable claim")
    return result


def finalize_attempt_result(
    result: Mapping[str, Any],
    *,
    outcome: str,
    retryable: bool,
    reason: str,
    target_proof: Mapping[str, Any] | None = None,
    next_owner: str | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    path = Path(str(result.get("result_path") or "")).expanduser().resolve()
    if path.parent != state_root() or path.suffix != ".json":
        raise LifecycleError("Integration attempt result path is outside the repository-owned state root")
    current = _read(path)
    if current is None or current.get("schema") != ATTEMPT_RESULT_SCHEMA:
        raise LifecycleError("Integration attempt result disappeared before classification")
    current.update(
        {
            "outcome": outcome,
            "retryable": bool(retryable),
            "reason": reason,
            "classified_at": _now(),
        }
    )
    if target_proof is not None:
        current["target_proof"] = dict(target_proof)
    if next_owner is not None:
        current["next_owner"] = next_owner
    if next_action is not None:
        current["next_action"] = next_action
    _atomic_write(path, current)
    return current


class LocalIntegrationFence(AbstractContextManager["LocalIntegrationFence"]):
    """One durable, crash-recoverable local owner for an exact PR/head Integration run.

    The OS lock is the concurrency admission invariant. The JSON claim is recovery state:
    a replacement that can acquire the lock knows no prior process still owns mutation and
    can reconstruct the last checkpoint instead of trusting chat/process memory.
    """

    def __init__(
        self,
        *,
        repository: str,
        pr_number: int,
        branch: str,
        head: str,
        review_id: int,
        task_ids: list[str],
        main_sha: str,
        handoff_comment_id: int,
        handoff_key_value: str,
        target_branch: str | None = None,
        root: Path | None = None,
    ) -> None:
        if pr_number <= 0 or review_id <= 0 or handoff_comment_id <= 0:
            raise LifecycleError("local Integration claim requires positive PR/review/handoff ids")
        if FULL_SHA_RE.fullmatch(head) is None or FULL_SHA_RE.fullmatch(main_sha) is None:
            raise LifecycleError("local Integration claim requires exact head/main SHAs")
        self.repository = repository
        self.pr_number = pr_number
        self.branch = branch
        self.head = head.lower()
        self.review_id = review_id
        self.task_ids = list(task_ids)
        self.main_sha = main_sha.lower()
        self.target_branch = str(target_branch or "").strip() or "main"
        self.handoff_comment_id = handoff_comment_id
        self.handoff_key = handoff_key_value
        self.root = (root or state_root()).resolve()
        prefix = f"{_safe_repository(repository)}--pr-{pr_number}--{self.head}"
        self.state_path = self.root / f"{prefix}.json"
        self.lock_path = self.root / f"{prefix}.lock"
        self._handle = None
        self.state: dict[str, Any] | None = None

    def recovery_state(self) -> dict[str, Any] | None:
        return _read(self.state_path)

    def liveness(self) -> dict[str, Any]:
        state = self.recovery_state() or {}
        lock_held = _lock_held(self.lock_path)
        worker_alive = _pid_alive(state.get("worker_pid"))
        child_alive = _pid_alive(state.get("child_pid"))
        process_alive = worker_alive or child_alive
        return {
            "lock_held": lock_held,
            "worker_alive": worker_alive,
            "child_alive": child_alive,
            "process_alive": process_alive,
            "running": bool(lock_held and process_alive),
            "worker_pid": state.get("worker_pid"),
            "child_pid": state.get("child_pid"),
        }

    def acquire(self) -> bool:
        _ensure_root(self.root)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self._handle = handle
        prior = _read(self.state_path)
        if prior is not None:
            expected = {
                "schema": CLAIM_SCHEMA,
                "repository": self.repository,
                "pr_number": self.pr_number,
                "branch": self.branch,
                "head": self.head,
            }
            for key, value in expected.items():
                if prior.get(key) != value:
                    self.release()
                    raise LifecycleError(
                        f"local Integration recovery claim identity changed at {key}: {prior.get(key)!r} != {value!r}"
                    )
            prior_target = str(prior.get("target_branch") or "").strip()
            if prior_target and prior_target != self.target_branch:
                self.release()
                raise LifecycleError(
                    f"local Integration recovery target changed: {prior_target!r} != {self.target_branch!r}"
                )
        generation = int(prior.get("generation") or 0) + 1 if prior else 1
        claim_id = str(uuid.uuid4())
        history = list(prior.get("history") or []) if prior else []
        recovery = None
        if prior:
            recovery = {
                "claim_id": prior.get("claim_id"),
                "generation": prior.get("generation"),
                "phase": prior.get("phase"),
                "status": prior.get("status"),
                "worktree": prior.get("worktree"),
                "current_head": prior.get("current_head"),
                "reconciliation_occurred": prior.get("reconciliation_occurred"),
                "next_action": prior.get("next_action"),
                "review_id": prior.get("review_id"),
                "task_ids": list(prior.get("task_ids") or []),
                "main_sha": prior.get("main_sha"),
                "target_branch": prior.get("target_branch"),
                "handoff_comment_id": prior.get("handoff_comment_id"),
                "handoff_key": prior.get("handoff_key"),
                "attempt_id": prior.get("attempt_id"),
                "attempt_result_path": prior.get("attempt_result_path"),
                "process_exit_code": prior.get("process_exit_code"),
            }
            history.append({**recovery, "superseded_at": _now()})
        self.state = {
            "schema": CLAIM_SCHEMA,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "branch": self.branch,
            "head": self.head,
            "review_id": self.review_id,
            "task_ids": self.task_ids,
            "main_sha": self.main_sha,
            "target_branch": self.target_branch,
            "handoff_comment_id": self.handoff_comment_id,
            "handoff_key": self.handoff_key,
            "claim_id": claim_id,
            "generation": generation,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": _now(),
            "status": "active",
            "phase": "claimed",
            "current_head": self.head,
            "reconciliation_occurred": False,
            "worktree": None,
            "next_action": "re-read live GitHub and owning Asana authority before Integration mutation",
            "recovery": recovery,
            "history": history,
        }
        _atomic_write(self.state_path, self.state)
        return True

    def lock_fd(self) -> int:
        if self._handle is None:
            raise LifecycleError("local Integration fence is not acquired")
        return self._handle.fileno()

    def payload(self) -> dict[str, Any]:
        if self.state is None:
            raise LifecycleError("local Integration fence is not acquired")
        current = _read(self.state_path)
        if current is None or current.get("claim_id") != self.state.get("claim_id"):
            raise LifecycleError("local Integration claim disappeared or changed while owned")
        return {**current, "state_path": str(self.state_path), "lock_path": str(self.lock_path)}

    def update(self, **fields: Any) -> dict[str, Any]:
        if self.state is None:
            raise LifecycleError("local Integration fence is not acquired")
        current = _read(self.state_path)
        if current is None or current.get("claim_id") != self.state.get("claim_id"):
            raise LifecycleError("local Integration claim changed while owned")
        current.update(fields)
        _atomic_write(self.state_path, current)
        self.state = current
        return current

    def finish(
        self,
        *,
        status: str,
        phase: str,
        next_action: str,
        current_head: str | None = None,
        merge_sha: str | None = None,
    ) -> None:
        if self.state is None:
            return
        current = _read(self.state_path)
        if current is None or current.get("claim_id") != self.state.get("claim_id"):
            raise LifecycleError("local Integration claim changed before completion checkpoint")
        current.update(
            {
                "status": status,
                "phase": phase,
                "next_action": next_action,
                "released_at": _now(),
            }
        )
        if current_head is not None:
            if FULL_SHA_RE.fullmatch(current_head) is None:
                raise LifecycleError("local Integration completion checkpoint requires an exact current head SHA")
            current["current_head"] = current_head.lower()
        if merge_sha:
            if FULL_SHA_RE.fullmatch(merge_sha) is None:
                raise LifecycleError("local Integration merge checkpoint requires an exact merge SHA")
            current["merge_sha"] = merge_sha.lower()
        _atomic_write(self.state_path, current)
        self.state = current

    def detach(self) -> None:
        """Drop only this process' FD after a worker inherited it; never unlock the shared flock."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def release(self) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None

    def __enter__(self) -> "LocalIntegrationFence":
        if not self.acquire():
            raise LifecycleError("local Integration execution is already owned for this exact PR/head")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class LocalIntegrationLauncher:
    """Run local Integration while preserving real flock ownership and complete attempt evidence."""

    def __init__(self, command: str | None) -> None:
        self.command = command

    def dispatch(self, context: dict[str, Any], *, lock_fd: int | None = None) -> None:
        """Synchronous path retained for bounded local certification."""
        if not self.command:
            raise LifecycleError("local Integration launcher command is unavailable")
        env = os.environ.copy()
        pass_fds: tuple[int, ...] = ()
        if lock_fd is not None:
            try:
                os.fstat(lock_fd)
            except OSError as exc:
                raise LifecycleError("local Integration launcher received an invalid fence fd") from exc
            env["DISH_LOCAL_INTEGRATION_LOCK_FD"] = str(lock_fd)
            pass_fds = (lock_fd,)
        completed = subprocess.run(
            shlex.split(self.command),
            input=json.dumps(context),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
            pass_fds=pass_fds,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise LifecycleError(
                f"local Integration launcher failed with exit {completed.returncode}"
                f"{': ' + detail if detail else ''}"
            )

    @staticmethod
    def _prune_attempt_artifacts(root: Path, prefix: str) -> None:
        results = sorted(root.glob(f"{prefix}.attempt-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in results[ATTEMPT_RETENTION:]:
            stem = stale.with_suffix("")
            for path in (
                stale,
                Path(str(stem) + ".stdout.log"),
                Path(str(stem) + ".stderr.log"),
                Path(str(stem) + ".context.json"),
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def dispatch_background(self, context: dict[str, Any], *, fence: LocalIntegrationFence) -> dict[str, Any]:
        """Start one fenced worker and return immediately so the controller can keep reconciling."""
        if not self.command:
            raise LifecycleError("local Integration launcher command is unavailable")
        claim = fence.payload()
        generation = int(claim["generation"])
        if generation > MAX_INTEGRATION_ATTEMPTS:
            raise LifecycleError(
                f"bounded Integration attempt budget exhausted ({MAX_INTEGRATION_ATTEMPTS}) for this exact PR/head"
            )
        root = fence.root
        prefix = fence.state_path.stem
        attempt_id = f"{claim['claim_id']}:{generation}"
        base = root / f"{prefix}.attempt-{generation}"
        context_path = Path(str(base) + ".context.json")
        result_path = Path(str(base) + ".json")
        stdout_path = Path(str(base) + ".stdout.log")
        stderr_path = Path(str(base) + ".stderr.log")
        _atomic_write(context_path, context)
        fence.update(
            attempt_id=attempt_id,
            attempt_result_path=str(result_path),
            attempt_context_path=str(context_path),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            phase="claimed",
            status="active",
            next_action="Integration worker is starting; controller reconciliation remains independent",
        )
        worker_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_worker",
            "--command",
            self.command,
            "--context-path",
            str(context_path),
            "--result-path",
            str(result_path),
            "--stdout-path",
            str(stdout_path),
            "--stderr-path",
            str(stderr_path),
            "--claim-path",
            str(fence.state_path),
            "--claim-id",
            str(claim["claim_id"]),
            "--attempt-id",
            attempt_id,
            "--lock-fd",
            str(fence.lock_fd()),
        ]
        try:
            worker = subprocess.Popen(
                worker_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(fence.lock_fd(),),
            )
        except OSError as exc:
            raise LifecycleError(f"cannot start fenced Integration worker: {exc}") from exc
        fence.update(
            worker_pid=worker.pid,
            worker_started_at=_now(),
            next_action="Integration child is running under the inherited flock; controller continues reconciliation",
        )
        fence.detach()
        self._prune_attempt_artifacts(root, prefix)
        return {
            "attempt_id": attempt_id,
            "generation": generation,
            "worker_pid": worker.pid,
            "result_path": str(result_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }


def _update_worker_claim(claim_path: Path, claim_id: str, **fields: Any) -> None:
    state = _read(claim_path)
    if state is None or state.get("schema") != CLAIM_SCHEMA or state.get("claim_id") != claim_id:
        raise LifecycleError("Integration worker claim disappeared or changed")
    state.update(fields)
    state["checkpointed_at"] = _now()
    _atomic_write(claim_path, state)


def _worker(
    *,
    command: str,
    context_path: Path,
    result_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    claim_path: Path,
    claim_id: str,
    attempt_id: str,
    lock_fd: int,
) -> int:
    try:
        os.fstat(lock_fd)
    except OSError as exc:
        raise LifecycleError("Integration worker received an invalid inherited fence fd") from exc
    context = _read(context_path)
    if context is None:
        raise LifecycleError("Integration worker context is missing")
    started_at = _now()
    env = os.environ.copy()
    env["DISH_LOCAL_INTEGRATION_LOCK_FD"] = str(lock_fd)
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
        child = subprocess.Popen(
            shlex.split(command),
            stdin=subprocess.PIPE,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            env=env,
            close_fds=True,
            pass_fds=(lock_fd,),
        )
        _update_worker_claim(
            claim_path,
            claim_id,
            worker_pid=os.getpid(),
            child_pid=child.pid,
            phase="premerge",
            next_action="Integration child is active under real process + flock evidence",
        )
        child.communicate(json.dumps(context))
        exit_code = int(child.returncode)
    transient = transient_infrastructure_reason(str(stdout_path), str(stderr_path))
    terminal_detail = (_tail(stderr_path) or _tail(stdout_path)).strip()[-2000:]
    outcome = "PROCESS_EXIT_ZERO" if exit_code == 0 else "PROCESS_FAILED"
    retryable = bool(exit_code != 0 and transient)
    result = {
        "schema": ATTEMPT_RESULT_SCHEMA,
        "attempt_id": attempt_id,
        "claim_id": claim_id,
        "generation": int((_read(claim_path) or {}).get("generation") or 0),
        "repository": context.get("repository"),
        "task_ids": list(context.get("task_ids") or []),
        "pull_request": dict(context.get("pull_request") or {}),
        "target": dict(context.get("target") or {}),
        "started_at": started_at,
        "finished_at": _now(),
        "process_exit_code": exit_code,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "result_path": str(result_path),
        "outcome": outcome,
        "retryable": retryable,
        "terminal_detail": terminal_detail,
        "reason": (
            terminal_detail
            if terminal_detail
            else ("process exited zero; authoritative effect readback still required" if exit_code == 0 else f"process exited {exit_code}")
        ),
    }
    _atomic_write(result_path, result)
    _update_worker_claim(
        claim_path,
        claim_id,
        status="returned",
        phase="returned",
        child_pid=child.pid,
        process_exit_code=exit_code,
        attempt_result_path=str(result_path),
        next_action="controller must classify the durable attempt result against live authoritative effect readback",
    )
    return 0


def checkpoint_claim(
    *,
    claim_path: str,
    claim_id: str,
    phase: str,
    worktree: str | None = None,
    current_head: str | None = None,
    main_sha: str | None = None,
    next_action: str | None = None,
    merge_sha: str | None = None,
) -> dict[str, Any]:
    """Update recovery metadata from the currently fenced local Integration child."""
    if phase not in _PHASES:
        raise LifecycleError(f"unsupported local Integration checkpoint phase: {phase!r}")
    root = state_root()
    path = Path(claim_path).expanduser().resolve()
    if path.parent != root or path.suffix != ".json":
        raise LifecycleError("local Integration checkpoint path is outside the repository-owned state root")
    state = _read(path)
    if state is None or state.get("schema") != CLAIM_SCHEMA:
        raise LifecycleError("local Integration checkpoint claim is missing or invalid")
    if state.get("claim_id") != claim_id or state.get("status") != "active":
        raise LifecycleError("local Integration checkpoint does not match the current active claim")
    state["phase"] = phase
    state["checkpointed_at"] = _now()
    if worktree is not None:
        state["worktree"] = str(Path(worktree).expanduser().resolve())
    if current_head is not None:
        if FULL_SHA_RE.fullmatch(current_head) is None:
            raise LifecycleError("checkpoint current head must be an exact SHA")
        state["current_head"] = current_head.lower()
    if main_sha is not None:
        if FULL_SHA_RE.fullmatch(main_sha) is None:
            raise LifecycleError("checkpoint main SHA must be an exact SHA")
        state["observed_main_sha"] = main_sha.lower()
    if phase in {"reconciling", "reconciled"}:
        state["reconciliation_occurred"] = True
    if next_action is not None:
        state["next_action"] = next_action
    if merge_sha is not None:
        if FULL_SHA_RE.fullmatch(merge_sha) is None:
            raise LifecycleError("checkpoint merge SHA must be an exact SHA")
        state["merge_sha"] = merge_sha.lower()
    _atomic_write(path, state)
    return state


def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("mode")
    parser.add_argument("--command", required=True)
    parser.add_argument("--context-path", required=True)
    parser.add_argument("--result-path", required=True)
    parser.add_argument("--stdout-path", required=True)
    parser.add_argument("--stderr-path", required=True)
    parser.add_argument("--claim-path", required=True)
    parser.add_argument("--claim-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--lock-fd", required=True, type=int)
    return parser


def _main() -> int:
    args = _worker_parser().parse_args()
    if args.mode != "_worker":
        raise LifecycleError(f"unsupported local Integration helper mode: {args.mode!r}")
    return _worker(
        command=args.command,
        context_path=Path(args.context_path).expanduser().resolve(),
        result_path=Path(args.result_path).expanduser().resolve(),
        stdout_path=Path(args.stdout_path).expanduser().resolve(),
        stderr_path=Path(args.stderr_path).expanduser().resolve(),
        claim_path=Path(args.claim_path).expanduser().resolve(),
        claim_id=args.claim_id,
        attempt_id=args.attempt_id,
        lock_fd=args.lock_fd,
    )


if __name__ == "__main__":  # pragma: no cover - exercised through the spawned worker path
    try:
        raise SystemExit(_main())
    except LifecycleError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
