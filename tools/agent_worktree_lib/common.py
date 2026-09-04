from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

from typing import Any
from urllib.parse import urlparse

EXPECTED_REPOSITORY = "marcogallotta/ai-tools"
EXPECTED_ORIGIN_ID = "github.com/marcogallotta/ai-tools"
SCHEMA_VERSION = 1
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
TASK_RE = re.compile(r"[0-9]+")
AGENT_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
DISPOSITIONS = {"merged", "closed", "abandoned", "superseded"}
FORBIDDEN_GIT_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EXEC_PATH",
    "GIT_EXTERNAL_DIFF",
    "GIT_SHALLOW_FILE",
    "GIT_IMPLICIT_WORK_TREE",
}


class AgentWorktreeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code: str, message: str) -> "NoReturn":
    raise AgentWorktreeError(code, message)


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def require_task_gid(value: str) -> str:
    if not TASK_RE.fullmatch(value):
        fail("INVALID_TASK", "task GID must contain digits only")
    return value


def require_agent_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not AGENT_ID_RE.fullmatch(value):
        fail("INVALID_AGENT_ID", "agent id may contain only letters, digits, '.', '_' and '-'")
    return value


# Env vars each host exposes to its own Bash subprocesses, carrying the same
# session identifier the host's own PreToolUse hooks receive as payload
# `session_id`. Guard-hook auto-allow logic (hooks/asana-write-guard,
# hooks/agent-reground) matches identity-file agent_id against that live
# payload value, so an --agent-id typed by hand (or omitted) here almost
# never matches it in practice - resolving from the same env var the hook
# itself will see is what makes the two sides agree.
AGENT_SESSION_ENV_VARS = ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")


def resolve_agent_id(value: str | None) -> str | None:
    if value is None:
        for env_var in AGENT_SESSION_ENV_VARS:
            found = os.environ.get(env_var)
            if found:
                value = found
                break
    return require_agent_id(value)


def require_full_sha(value: str, label: str) -> str:
    if not FULL_SHA_RE.fullmatch(value):
        fail("INVALID_SHA", f"{label} must be an exact 40-character lowercase Git commit SHA")
    return value


def reject_repository_overrides() -> None:
    present = sorted(
        name
        for name in os.environ
        if name in FORBIDDEN_GIT_ENV or name.startswith("GIT_CONFIG_")
    )
    if present:
        fail(
            "GIT_ENV_OVERRIDE",
            "repository/configuration Git environment override(s) are not allowed: "
            + ", ".join(present)
            + ". Unset them and retry.",
        )


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


class GitRunner:
    def __init__(self, candidate_worktree: Path | None = None):
        found = shutil.which("git")
        if found is None:
            fail("GIT_NOT_FOUND", "git executable not found on PATH")
        self.exe = Path(found).resolve()
        if candidate_worktree is not None and path_is_within(self.exe, candidate_worktree.resolve()):
            fail("GIT_EXECUTABLE_AMBIGUOUS", f"git executable resolves inside candidate worktree: {self.exe}")
        self._temp = tempfile.TemporaryDirectory(prefix="agent-worktree-git-")
        self.hooks_dir = Path(self._temp.name) / "empty-hooks"
        self.hooks_dir.mkdir(mode=0o700)

    def close(self) -> None:
        self._temp.cleanup()

    def check_candidate(self, candidate_worktree: Path) -> None:
        if path_is_within(self.exe, candidate_worktree.resolve()):
            fail("GIT_EXECUTABLE_AMBIGUOUS", f"git executable resolves inside candidate worktree: {self.exe}")

    def run(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(self.exe),
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={self.hooks_dir}",
            "-C",
            str(cwd),
            *args,
        ]
        completed = subprocess.run(
            command,
            text=True,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.rstrip() or completed.stdout.rstrip() or "no command output"
            fail("GIT_COMMAND_FAILED", f"git command failed ({completed.returncode}): {' '.join(args)}\n{detail}")
        return completed

    def path(self, cwd: Path, *args: str) -> Path:
        raw = self.run(cwd, "rev-parse", "--path-format=absolute", *args).stdout.strip()
        return Path(raw).resolve()

    def sha(self, cwd: Path, ref: str) -> str:
        return require_full_sha(self.run(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip(), ref)


@dataclass(frozen=True)
class Repository:
    source_top: Path
    primary_top: Path
    common_dir: Path
    origin_url: str
    origin_id: str


@dataclass(frozen=True)
class WorktreeIdentity:
    path: Path
    git_dir: Path
    common_dir: Path
    branch: str
    head: str
    dirty: bool


def parse_worktrees_z(raw: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for token in raw.split("\0"):
        if token == "":
            if current:
                records.append(current)
                current = {}
            continue
        key, sep, value = token.partition(" ")
        current[key] = value if sep else "true"
    if current:
        records.append(current)
    return records


def worktree_records(runner: GitRunner, cwd: Path) -> list[dict[str, str]]:
    completed = runner.run(cwd, "worktree", "list", "--porcelain", "-z")
    return parse_worktrees_z(completed.stdout)


def find_worktree_record(records: list[dict[str, str]], path: Path) -> dict[str, str] | None:
    target = path.resolve()
    for record in records:
        raw = record.get("worktree")
        if raw and Path(raw).resolve() == target:
            return record
    return None


def reject_url_rewrites(runner: GitRunner, cwd: Path) -> None:
    result = runner.run(cwd, "config", "--show-origin", "--get-regexp", r"^url\..*\.", check=False)
    if result.returncode not in (0, 1):
        fail("GIT_CONFIG_AMBIGUOUS", "could not inspect Git URL rewrite configuration")
    rewrites: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) < 2:
            continue
        key = fields[1].lower()
        if key.endswith(".insteadof") or key.endswith(".pushinsteadof"):
            rewrites.append(key)
    if rewrites:
        fail(
            "GIT_URL_REWRITE",
            "Git url.*.insteadOf/pushInsteadOf rewriting is not allowed for agent-worktree origin verification: "
            + ", ".join(sorted(set(rewrites))),
        )


def normalize_origin(url: str) -> str:
    value = url.strip()
    if not value:
        fail("ORIGIN_IDENTITY", "origin URL is empty")

    path: str | None = None
    if value.startswith(("https://", "ssh://")):
        parsed = urlparse(value)
        if parsed.hostname is None or parsed.hostname.lower() != "github.com":
            fail("ORIGIN_IDENTITY", "origin must resolve to github.com/marcogallotta/ai-tools")
        if parsed.password is not None:
            fail("ORIGIN_IDENTITY", "origin URL must not embed credentials")
        if parsed.scheme == "https" and parsed.username is not None:
            fail("ORIGIN_IDENTITY", "HTTPS origin URL must not embed a username/token")
        if parsed.scheme == "ssh" and parsed.port not in (None, 22):
            fail("ORIGIN_IDENTITY", "SSH origin uses an unexpected port")
        path = parsed.path.lstrip("/")
    else:
        match = re.fullmatch(r"(?:[^@\s]+@)?github\.com:(.+)", value)
        if match:
            path = match.group(1)
    if path is None:
        fail("ORIGIN_IDENTITY", "origin must use an accepted GitHub HTTPS/SSH URL form")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    identity = f"github.com/{path.lower()}"
    if identity != EXPECTED_ORIGIN_ID:
        fail("ORIGIN_IDENTITY", f"origin identifies {identity!r}; expected {EXPECTED_ORIGIN_ID!r}")
    return identity


def current_symbolic_branch(runner: GitRunner, cwd: Path) -> str | None:
    result = runner.run(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return None
    fail("GIT_IDENTITY", "could not resolve symbolic HEAD")
