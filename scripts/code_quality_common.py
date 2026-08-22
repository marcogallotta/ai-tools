"""Shared deterministic policy/file primitives for the Dish code-quality ratchet."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = Path(os.environ.get("DISH_CODE_QUALITY_TOOL_ROOT", str(ROOT))).resolve()
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

SCHEMA = "dish-code-quality-result-v1"
MARKER_RE = re.compile(r"<!-- dish-code-quality-result:v1 head=([0-9a-f]{40}) digest=([0-9a-f]{64}) -->")
JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GateError(RuntimeError):
    pass


def _run(argv: list[str], *, cwd: Path, allow: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode not in allow:
        raise GateError(f"{' '.join(argv)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


def _git(repo: Path, *args: str, allow: tuple[int, ...] = (0,)) -> str:
    return _run(["git", *args], cwd=repo, allow=allow).stdout.strip()


def _sha(value: str, label: str) -> str:
    value = value.strip().lower()
    if not SHA_RE.fullmatch(value):
        raise GateError(f"{label} must be an exact lowercase 40-hex SHA")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _git_file(repo: Path, sha: str, path: str) -> bytes | None:
    proc = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode == 0:
        return proc.stdout
    if b"does not exist" in proc.stderr or b"exists on disk, but not in" in proc.stderr or b"Path '" in proc.stderr:
        return None
    raise GateError(f"git show {sha}:{path} failed: {proc.stderr.decode(errors='replace').strip()}")


def _blob_size(repo: Path, sha: str, path: str) -> int | None:
    if _git_file(repo, sha, path) is None:
        return None
    return int(_git(repo, "cat-file", "-s", f"{sha}:{path}"))




def _changed_pairs(repo: Path, base: str, head: str) -> tuple[tuple[str | None, str | None], ...]:
    proc = subprocess.run(["git", "diff", "--name-status", "-z", "--find-renames", base, head], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise GateError(f"git diff failed: {proc.stderr.decode(errors='replace').strip()}")
    fields = proc.stdout.decode("utf-8").split("\0")
    if fields and fields[-1] == "": fields.pop()
    pairs: list[tuple[str | None, str | None]] = []
    index = 0
    while index < len(fields):
        status = fields[index]; index += 1
        if status.startswith("R"):
            old, new = fields[index], fields[index + 1]; index += 2
            pairs.append((old, new))
        elif status.startswith("C"):
            _source, new = fields[index], fields[index + 1]; index += 2
            pairs.append((None, new))
        else:
            path = fields[index]; index += 1
            pairs.append((path if status[0] != "A" else None, path if status[0] != "D" else None))
    return tuple(pairs)

def _nonblank_lines(data: bytes | None, path: str) -> int | None:
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateError(f"{path} is not valid UTF-8") from exc
    return sum(1 for line in text.splitlines() if line.strip())


def _load_policy(repo: Path, comparison_base: str, head: str) -> tuple[dict[str, Any], str, str, bool]:
    path = "ci/code-quality.toml"
    raw = _git_file(repo, comparison_base, path)
    bootstrap = raw is None
    source = head if bootstrap else comparison_base
    raw = _git_file(repo, source, path)
    if raw is None:
        raise GateError("code-quality policy is missing from both comparison base and head")
    try:
        policy = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GateError("code-quality policy is not valid UTF-8 TOML") from exc
    if int(policy.get("version", 0)) != 1:
        raise GateError("unsupported code-quality policy version")
    return policy, source, hashlib.sha256(raw).hexdigest(), bootstrap


def _load_registry(repo: Path, source: str, policy: dict[str, Any]) -> tuple[dict[str, Any], str]:
    path = str(policy["tracked_files"]["generated_registry"])
    raw = _git_file(repo, source, path)
    if raw is None:
        raise GateError(f"generated registry missing at policy source: {path}")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError("generated registry is not valid JSON") from exc
    if value.get("schema") != "dish-code-quality-generated-registry-v1" or not isinstance(value.get("entries"), list):
        raise GateError("generated registry schema is invalid")
    approved: dict[str, Any] = {}
    required = {"path", "purpose", "consumer", "why_tracked", "representation", "materializer", "integrity"}
    for entry in value["entries"]:
        if not isinstance(entry, dict) or not required.issubset(entry) or any(not str(entry[k]).strip() for k in required):
            raise GateError("generated registry entry is incomplete")
        path_value = str(entry["path"])
        if path_value in approved:
            raise GateError(f"duplicate generated registry path: {path_value}")
        approved[path_value] = entry
    return approved, hashlib.sha256(raw).hexdigest()


def _looks_generated(path: str, data: bytes | None, policy: dict[str, Any]) -> bool:
    if Path(path).suffix.lower() in set(policy["tracked_files"]["likely_generated_extensions"]):
        return True
    if data is None or len(data) > 200_000:
        return False
    text = data.decode("utf-8", errors="ignore")[:3000].lower()
    return "generated" in text and ("do not edit" in text or "autogenerated" in text or "auto-generated" in text)


def _file_findings(repo: Path, base: str, head: str, pairs: Iterable[tuple[str | None, str | None]], policy: dict[str, Any], registry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    max_lines = int(policy["python_size"]["max_nonblank_lines"])
    source_exts = set(policy["tracked_files"]["source_extensions"])
    manageable = int(policy["tracked_files"]["manageability_bytes"])
    hard = int(policy["tracked_files"]["operational_hard_bytes"])
    for base_path, path in pairs:
        if path is None:
            continue
        head_data = _git_file(repo, head, path)
        if head_data is None:
            continue
        base_data = _git_file(repo, base, base_path) if base_path is not None else None
        generated = _looks_generated(path, head_data, policy)
        if generated and path not in registry:
            failures.append({"kind": "generated_unregistered", "path": path})
        if path.endswith(".py") and not generated:
            before = _nonblank_lines(base_data, path)
            after = _nonblank_lines(head_data, path)
            assert after is not None
            if after > max_lines and (before is None or before <= max_lines or after > before):
                failures.append({"kind": "python_size", "path": path, "base": before, "head": after, "limit": max_lines})
            elif after > max_lines:
                signals.append({"kind": "python_size_legacy", "path": path, "base": before, "head": after, "limit": max_lines})
        if Path(path).suffix.lower() not in source_exts:
            before_size = len(base_data) if base_data is not None else None
            after_size = len(head_data)
            grew = before_size is None or after_size > before_size
            if grew and after_size > hard:
                failures.append({"kind": "tracked_file_hard_size", "path": path, "base": before_size, "head": after_size, "limit": hard})
            elif grew and after_size > manageable:
                signals.append({"kind": "tracked_file_manageability", "path": path, "base": before_size, "head": after_size, "limit": manageable})
    return failures, signals


@contextmanager
def _worktrees(repo: Path, base: str, head: str):
    with tempfile.TemporaryDirectory(prefix="dish-code-quality-") as temp:
        root = Path(temp)
        base_dir, head_dir = root / "base", root / "head"
        _run(["git", "worktree", "add", "--detach", str(base_dir), base], cwd=repo)
        try:
            _run(["git", "worktree", "add", "--detach", str(head_dir), head], cwd=repo)
            try:
                yield base_dir, head_dir
            finally:
                _run(["git", "worktree", "remove", "--force", str(head_dir)], cwd=repo)
        finally:
            _run(["git", "worktree", "remove", "--force", str(base_dir)], cwd=repo)


def _tool(executable: str) -> str:
    path = (TOOL_ROOT / executable).resolve() if "/" in executable else Path(executable)
    return str(path)
