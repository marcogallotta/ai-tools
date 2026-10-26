"""Fresh, SHA-pinned Honest assets, independent of any working checkout."""
from __future__ import annotations

import fcntl
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

REMOTE_URL = "git@github.com:marcogallotta/honest-pantry.git"
SERVED_REF = "refs/heads/served-main"
SSH_COMMAND = "/usr/bin/ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=10"
LOCK_TIMEOUT = 10.0
REQUEST_TIMEOUT = 90.0
_LOCK = threading.Lock()
_DEADLINE: ContextVar[float | None] = ContextVar("honest_deadline", default=None)


def _error(operation: str, detail: str) -> RuntimeError:
    # Do not expose embedded URL credentials, terminal controls, or unbounded stderr.
    detail = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", detail)
    detail = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", detail)
    return RuntimeError(f"Honest Pantry {operation} failed; no files were served ({detail[:2048]})")


def git(store_path: Path, *args: str, timeout: float = 10) -> bytes:
    deadline = _DEADLINE.get()
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
    if timeout <= 0:
        raise _error(args[0], "request deadline exceeded")
    # Inherited Git configuration must not redirect the store, fetch, or object reads.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND=SSH_COMMAND,
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    command = ["/usr/bin/git", "--git-dir", str(store_path), "-c", "core.hooksPath=/dev/null",
               "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args]
    try:
        # Disk-backed stderr bounds memory collection as well as the returned diagnostic.
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                                       env=env, start_new_session=True)
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise _error(args[0], f"timed out after {timeout:g}s; subprocess group stopped") from exc
            if process.returncode:
                errors.seek(0)
                detail = errors.read(2048).decode("utf-8", "replace").strip()
                raise _error(args[0], f"exit {process.returncode}: {detail}")
            return output
    except OSError as exc:
        raise _error(args[0], str(exc)) from exc


def _initialize(store_path: Path) -> None:
    marker = store_path.with_name(store_path.name + ".initializing")
    if not store_path.exists():
        marker.touch(mode=0o600, exist_ok=True)
    if marker.exists():
        # Repeating init repairs an interrupted first init; never recreate an established store.
        git(store_path, "init", "--bare", "--template=", str(store_path))
    if git(store_path, "rev-parse", "--is-bare-repository").strip() != b"true":
        raise _error("validation", "store must be a bare Git repository")
    marker.unlink(missing_ok=True)


@contextmanager
def snapshot(store_path: Path):
    """Serialize init/fetch/read across threads and processes; never serve stale assets."""
    store_path = Path(store_path)
    if not store_path.is_absolute():
        raise _error("configuration", "store path must be absolute")
    deadline_token = _DEADLINE.set(time.monotonic() + REQUEST_TIMEOUT)
    acquired = _LOCK.acquire(timeout=LOCK_TIMEOUT)
    if not acquired:
        _DEADLINE.reset(deadline_token)
        raise _error("lock", "timed out waiting for another Honest request")
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Stable external pathname: never remove/replace this file, including during recovery.
        with store_path.with_name(store_path.name + ".lock").open("a+b") as lock:
            lock_deadline = min(_DEADLINE.get(), time.monotonic() + LOCK_TIMEOUT)
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= lock_deadline:
                        raise _error("lock", "timed out waiting for another Honest process")
                    time.sleep(0.05)
            try:
                _initialize(store_path)
                git(store_path, "fetch", "--no-tags", "--no-recurse-submodules", "--no-prune",
                    "--no-prune-tags", "--no-auto-maintenance", "--no-write-fetch-head", "--refmap=",
                    REMOTE_URL, f"refs/heads/main:{SERVED_REF}", timeout=30)
                sha = git(store_path, "rev-parse", "--verify", f"{SERVED_REF}^{{commit}}").decode().strip()
                yield sha
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError as exc:
        raise _error("store access", str(exc)) from exc
    finally:
        _LOCK.release()
        _DEADLINE.reset(deadline_token)
