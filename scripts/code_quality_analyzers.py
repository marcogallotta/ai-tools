"""Pinned analyzer adapters for the Dish code-quality ratchet."""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from code_quality_common import GateError, _run, _tool, _worktrees

def _ruff_counts(worktree: Path, paths: list[str], policy: dict[str, Any]) -> dict[str, int]:
    if not paths:
        return {}
    exe = _tool("dish/.venv/bin/ruff")
    select = ",".join(policy["ruff"]["select"])
    proc = _run([exe, "check", "--isolated", "--no-cache", "--output-format", "json", "--select", select, *paths], cwd=worktree, allow=(0, 1))
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GateError("Ruff did not emit JSON") from exc
    counts = collections.Counter(str(row.get("code") or "") for row in rows if row.get("code"))
    return dict(sorted(counts.items()))


def _pyright_counts(worktree: Path, policy: dict[str, Any]) -> dict[str, int]:
    exe = _tool("dish/.venv/bin/pyright")
    cfg = {
        "typeCheckingMode": policy["pyright"]["type_checking_mode"],
        "include": policy["pyright"]["include"],
        "pythonVersion": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    for rule in policy["pyright"]["nonblocking_rules"]:
        cfg[str(rule)] = "none"
    config_path = worktree / ".dish-code-quality-pyright.json"
    config_path.write_text(json.dumps(cfg, sort_keys=True), encoding="utf-8")
    try:
        proc = _run([exe, "--outputjson", "--project", str(config_path)], cwd=worktree, allow=(0, 1))
    finally:
        config_path.unlink(missing_ok=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GateError("Pyright did not emit JSON") from exc
    counts: collections.Counter[str] = collections.Counter()
    for row in payload.get("generalDiagnostics", []):
        if row.get("severity") == "error":
            counts[str(row.get("rule") or "unclassified")] += 1
    return dict(sorted(counts.items()))


def _norm_fragment(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _jscpd_occurrences(worktree: Path, policy: dict[str, Any]) -> dict[str, set[tuple[str, int, int]]]:
    cfg = policy["jscpd"]
    with tempfile.TemporaryDirectory(prefix="jscpd-report-") as output:
        package = f"jscpd@{cfg['version']}"
        argv = ["npx", "--yes", package, "--reporters", "json", "--output", output, "--mode", str(cfg["mode"]), "--min-lines", str(cfg["min_lines"]), "--min-tokens", str(cfg["min_tokens"]), "--silent"]
        if cfg.get("ignore"):
            argv += ["--ignore", ",".join(cfg["ignore"])]
        argv += list(cfg["scan_paths"])
        _run(argv, cwd=worktree, allow=(0, 1))
        report = Path(output) / "jscpd-report.json"
        if not report.exists():
            raise GateError("jscpd JSON report is missing")
        payload = json.loads(report.read_text(encoding="utf-8"))
    found: dict[str, set[tuple[str, int, int]]] = collections.defaultdict(set)
    for duplicate in payload.get("duplicates", []):
        fragment = _norm_fragment(str(duplicate.get("fragment") or ""))
        if not fragment:
            continue
        digest = hashlib.sha256(fragment.encode()).hexdigest()
        for key in ("firstFile", "secondFile"):
            loc = duplicate.get(key) or {}
            path = os.path.relpath(str(loc.get("name") or ""), worktree).replace(os.sep, "/")
            found[digest].add((path, int(loc.get("start") or 0), int(loc.get("end") or 0)))
    return found


def _positive_deltas(base: dict[str, int], head: dict[str, int]) -> dict[str, int]:
    return {key: head[key] - base.get(key, 0) for key in sorted(head) if head[key] > base.get(key, 0)}


def _run_analyzers(repo: Path, base: str, head: str, changed: tuple[str, ...], policy: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, float]]:
    results: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    timings: dict[str, float] = {}
    with _worktrees(repo, base, head) as (base_dir, head_dir):
        changed_py_base = [p for p in changed if p.endswith(".py") and (base_dir / p).is_file()]
        changed_py_head = [p for p in changed if p.endswith(".py") and (head_dir / p).is_file()]
        start = time.monotonic(); rb = _ruff_counts(base_dir, changed_py_base, policy); rh = _ruff_counts(head_dir, changed_py_head, policy); timings["ruff"] = time.monotonic() - start
        delta = _positive_deltas(rb, rh); results["ruff"] = {"scope": "changed-python-paths", "base": rb, "head": rh, "delta": delta}
        if delta: failures.append({"kind": "ruff_net_increase", "delta": delta})
        start = time.monotonic(); pb = _pyright_counts(base_dir, policy); ph = _pyright_counts(head_dir, policy); timings["pyright"] = time.monotonic() - start
        delta = _positive_deltas(pb, ph); results["pyright"] = {"scope": "whole-configured-python-project", "base": pb, "head": ph, "delta": delta}
        if delta: failures.append({"kind": "pyright_net_increase", "delta": delta})
        start = time.monotonic(); jb = _jscpd_occurrences(base_dir, policy); jh = _jscpd_occurrences(head_dir, policy); timings["jscpd"] = time.monotonic() - start
        changed_set = set(changed); clone_delta: dict[str, int] = {}
        for digest, occurrences in jh.items():
            excess = len(occurrences) - len(jb.get(digest, set()))
            if excess > 0 and any(path in changed_set for path, _, _ in occurrences):
                clone_delta[digest] = excess
        results["jscpd"] = {"scope": "whole-handwritten-source", "delta": dict(sorted(clone_delta.items()))}
        if clone_delta: failures.append({"kind": "jscpd_clone_increase", "delta": dict(sorted(clone_delta.items()))})
    return results, failures, timings
