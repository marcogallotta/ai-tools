"""Durable local-only Integration handoff and single-owner execution fence."""
from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import shlex
import tempfile
from typing import Any, Mapping
import uuid

from pr_lifecycle_support import FULL_SHA_RE, LifecycleError


HANDOFF_MARKER = "dish-local-integration-handoff:v1"
CLAIM_SCHEMA = "dish-local-integration-claim-v1"
HANDOFF_SCHEMA = "dish-pr-local-integration-v1"
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
        self.handoff_comment_id = handoff_comment_id
        self.handoff_key = handoff_key_value
        self.root = (root or state_root()).resolve()
        prefix = f"{_safe_repository(repository)}--pr-{pr_number}--{self.head}"
        self.state_path = self.root / f"{prefix}.json"
        self.lock_path = self.root / f"{prefix}.lock"
        self._handle = None
        self.state: dict[str, Any] | None = None

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
                "review_id": self.review_id,
                "main_sha": self.main_sha,
                "handoff_comment_id": self.handoff_comment_id,
                "handoff_key": self.handoff_key,
            }
            for key, value in expected.items():
                if prior.get(key) != value:
                    self.release()
                    raise LifecycleError(
                        f"local Integration recovery claim identity changed at {key}: {prior.get(key)!r} != {value!r}"
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
        return {**current, "state_path": str(self.state_path)}

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
    """Run the configured local Integration agent while preserving the fence across parent failure.

    For final Integration the inherited lock FD keeps the kernel flock alive until the
    child exits even if the dispatcher process crashes. Certification may use the same
    launcher without a lock FD because it is non-landing evidence execution.
    """

    def __init__(self, command: str | None) -> None:
        self.command = command

    def dispatch(self, context: dict[str, Any], *, lock_fd: int | None = None) -> None:
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
