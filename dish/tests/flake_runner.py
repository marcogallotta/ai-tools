"""Reproducible flaky-test detection commands for Dish.

Run from ``dish/`` with a dedicated environment created from
``requirements-flake.txt``::

    .venv-flake/bin/python -m tests.flake_runner --help
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import secrets
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_ROOT = ROOT / ".test-artifacts" / "flakes"
DEFAULT_RANDOM_RUNS = 5
DEFAULT_STRESS_RUNS = 20
DEFAULT_QUARANTINE_RUNS = 20
DEFAULT_FRESH_REPEATS = 30
DEFAULT_SAME_PROCESS_REPEATS = 50


@dataclass(frozen=True)
class CommandResult:
    label: str
    command: list[str]
    returncode: int
    junit: str | None = None
    seed: int | None = None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _artifact_dir(mode: str, requested: str | None) -> Path:
    path = Path(requested) if requested else DEFAULT_ARTIFACT_ROOT / f"{_utc_stamp()}-{mode}"
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=False)
    return path


def _hash_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_sha() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _environment_metadata(mode: str) -> dict[str, object]:
    return {
        "mode": mode,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cwd": str(ROOT),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "git_sha": _git_sha(),
        "requirements_test_sha256": _hash_file(ROOT / "requirements-test.txt"),
        "requirements_flake_sha256": _hash_file(ROOT / "requirements-flake.txt"),
    }


def _strip_separator(arguments: Sequence[str]) -> list[str]:
    if arguments and arguments[0] == "--":
        return list(arguments[1:])
    return list(arguments)


def _pytest_command(arguments: Iterable[str]) -> list[str]:
    return [sys.executable, "-m", "pytest", *arguments]


def _run_command(
    *,
    label: str,
    arguments: Sequence[str],
    artifact_dir: Path,
    seed: int | None = None,
) -> CommandResult:
    junit = artifact_dir / f"{label}.xml"
    command = _pytest_command([*arguments, f"--junitxml={junit}"])
    completed = subprocess.run(command, cwd=ROOT, check=False)
    return CommandResult(
        label=label,
        command=command,
        returncode=completed.returncode,
        junit=str(junit.relative_to(artifact_dir)),
        seed=seed,
    )


def _write_summary(
    *,
    artifact_dir: Path,
    metadata: dict[str, object],
    results: Sequence[CommandResult],
) -> int:
    payload = {
        **metadata,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "results": [asdict(result) for result in results],
        "passed": all(result.returncode == 0 for result in results),
    }
    (artifact_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if payload["passed"] else 1


def _seeds(count: int, explicit: Sequence[int]) -> list[int]:
    if explicit:
        if len(explicit) != count:
            raise ValueError("the number of --seed values must equal --runs")
        return list(explicit)
    return [secrets.randbelow(2**32) for _ in range(count)]


def run_rerun_detect(args: argparse.Namespace) -> int:
    artifact_dir = _artifact_dir("rerun-detect", args.artifacts)
    pytest_args = _strip_separator(args.pytest_args)
    result = _run_command(
        label="rerun-detect",
        arguments=[
            "-p",
            "no:randomly",
            "--reruns",
            str(args.reruns),
            "--fail-on-flaky",
            "--rerun-show-tracebacks",
            *pytest_args,
        ],
        artifact_dir=artifact_dir,
    )
    return _write_summary(
        artifact_dir=artifact_dir,
        metadata=_environment_metadata("rerun-detect"),
        results=[result],
    )


def run_random_order(args: argparse.Namespace) -> int:
    artifact_dir = _artifact_dir("random-order", args.artifacts)
    pytest_args = _strip_separator(args.pytest_args)
    seeds = _seeds(args.runs, args.seed)
    results = [
        _run_command(
            label=f"random-{index:02d}-seed-{seed}",
            arguments=[f"--randomly-seed={seed}", *pytest_args],
            artifact_dir=artifact_dir,
            seed=seed,
        )
        for index, seed in enumerate(seeds, start=1)
    ]
    metadata = _environment_metadata("random-order")
    metadata["seeds"] = seeds
    return _write_summary(artifact_dir=artifact_dir, metadata=metadata, results=results)


def run_stress(args: argparse.Namespace) -> int:
    artifact_dir = _artifact_dir("stress", args.artifacts)
    pytest_args = _strip_separator(args.pytest_args)
    seeds = _seeds(args.runs, args.seed)
    results = [
        _run_command(
            label=f"stress-{index:02d}-seed-{seed}",
            arguments=[
                "-m",
                args.marker,
                f"--randomly-seed={seed}",
                *pytest_args,
            ],
            artifact_dir=artifact_dir,
            seed=seed,
        )
        for index, seed in enumerate(seeds, start=1)
    ]
    metadata = _environment_metadata("stress")
    metadata.update({"seeds": seeds, "marker": args.marker})
    return _write_summary(artifact_dir=artifact_dir, metadata=metadata, results=results)


def run_repeat(args: argparse.Namespace) -> int:
    artifact_dir = _artifact_dir("repeat", args.artifacts)
    results: list[CommandResult] = []
    if args.same_process:
        results.append(
            _run_command(
                label="same-process",
                arguments=[
                    "-p",
                    "no:randomly",
                    "--count",
                    str(args.same_process),
                    "-x",
                    args.nodeid,
                ],
                artifact_dir=artifact_dir,
            )
        )
    for index in range(1, args.fresh_runs + 1):
        results.append(
            _run_command(
                label=f"fresh-{index:02d}",
                arguments=["-p", "no:randomly", args.nodeid],
                artifact_dir=artifact_dir,
            )
        )
    metadata = _environment_metadata("repeat")
    metadata.update(
        {
            "nodeid": args.nodeid,
            "same_process_repeats": args.same_process,
            "fresh_process_runs": args.fresh_runs,
        }
    )
    return _write_summary(artifact_dir=artifact_dir, metadata=metadata, results=results)


def run_parallel(args: argparse.Namespace) -> int:
    artifact_dir = _artifact_dir("parallel", args.artifacts)
    pytest_args = _strip_separator(args.pytest_args)
    results = [
        _run_command(
            label=f"xdist-{workers}",
            arguments=[
                "-p",
                "no:randomly",
                "-n",
                str(workers),
                "--dist",
                "loadfile",
                *pytest_args,
            ],
            artifact_dir=artifact_dir,
        )
        for workers in args.workers
    ]
    metadata = _environment_metadata("parallel")
    metadata["workers"] = args.workers
    return _write_summary(artifact_dir=artifact_dir, metadata=metadata, results=results)


def run_quarantine(args: argparse.Namespace) -> int:
    artifact_dir = _artifact_dir("quarantine", args.artifacts)
    pytest_args = _strip_separator(args.pytest_args)
    seeds = _seeds(args.runs, args.seed)
    results = [
        _run_command(
            label=f"quarantine-{index:02d}-seed-{seed}",
            arguments=["--quarantine", f"--randomly-seed={seed}", *pytest_args],
            artifact_dir=artifact_dir,
            seed=seed,
        )
        for index, seed in enumerate(seeds, start=1)
    ]
    metadata = _environment_metadata("quarantine")
    metadata["seeds"] = seeds
    return _write_summary(artifact_dir=artifact_dir, metadata=metadata, results=results)


def _call_name(node: ast.Call) -> str:
    try:
        return ast.unparse(node.func)
    except Exception:
        return "<unknown>"


def scan_flake_risks(root: Path = ROOT / "tests") -> list[dict[str, object]]:
    """Return a diagnostic inventory of common nondeterminism risk signals."""
    call_kinds = {
        "time.sleep": "real_sleep",
        "sleep": "possible_sleep",
        "threading.Thread": "thread",
        "Thread": "thread",
        "subprocess.Popen": "subprocess",
        "subprocess.run": "subprocess",
        "datetime.now": "wall_clock",
        "date.today": "wall_clock",
        "time.time": "wall_clock",
        "random.random": "randomness",
        "random.randint": "randomness",
        "random.choice": "randomness",
        "os.chdir": "working_directory",
        "monkeypatch.chdir": "working_directory",
        "monkeypatch.setenv": "environment",
        "monkeypatch.delenv": "environment",
    }
    findings: list[dict[str, object]] = []
    display_root = ROOT if root.is_relative_to(ROOT) else root.parent
    excluded = {"flake_policy.py", "flake_runner.py"}
    for path in sorted(root.rglob("*.py")):
        if path.name in excluded:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            kind = call_kinds.get(name)
            if (
                kind is None
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "join"
                and isinstance(node.func.value, (ast.Name, ast.Attribute))
            ):
                kind = "thread_or_process_join"
            if kind is None:
                continue
            findings.append(
                {
                    "file": str(path.relative_to(display_root)),
                    "line": node.lineno,
                    "kind": kind,
                    "call": name,
                }
            )
    return findings


def _risk_markdown(findings: Sequence[dict[str, object]]) -> str:
    lines = [
        "# Flake risk scan",
        "",
        "This is a candidate inventory, not proof that any listed test is flaky.",
        "",
        "| File | Line | Risk | Call |",
        "| --- | ---: | --- | --- |",
    ]
    lines.extend(
        f"| `{finding['file']}` | {finding['line']} | {finding['kind']} | "
        f"`{finding['call']}` |"
        for finding in findings
    )
    lines.append("")
    return "\n".join(lines)


def run_scan(args: argparse.Namespace) -> int:
    artifact_dir = _artifact_dir("scan", args.artifacts)
    findings = scan_flake_risks()
    (artifact_dir / "risks.json").write_text(
        json.dumps(findings, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (artifact_dir / "risks.md").write_text(
        _risk_markdown(findings), encoding="utf-8"
    )
    metadata = _environment_metadata("scan")
    metadata["finding_count"] = len(findings)
    return _write_summary(artifact_dir=artifact_dir, metadata=metadata, results=[])


def _add_common_artifact_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifacts",
        help="artifact directory; defaults below .test-artifacts/flakes",
    )


def _add_pytest_remainder(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="extra pytest arguments after --",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    rerun = subparsers.add_parser(
        "rerun-detect", help="fail if a test passes only after a rerun"
    )
    rerun.add_argument("--reruns", type=int, default=2)
    _add_common_artifact_argument(rerun)
    _add_pytest_remainder(rerun)
    rerun.set_defaults(handler=run_rerun_detect)

    random_order = subparsers.add_parser(
        "random-order", help="run fresh full-suite processes with recorded random seeds"
    )
    random_order.add_argument("--runs", type=int, default=DEFAULT_RANDOM_RUNS)
    random_order.add_argument("--seed", type=int, action="append", default=[])
    _add_common_artifact_argument(random_order)
    _add_pytest_remainder(random_order)
    random_order.set_defaults(handler=run_random_order)

    stress = subparsers.add_parser(
        "stress", help="repeat marked concurrency/recovery tests in fresh processes"
    )
    stress.add_argument("--runs", type=int, default=DEFAULT_STRESS_RUNS)
    stress.add_argument("--seed", type=int, action="append", default=[])
    stress.add_argument("--marker", default="flake_stress")
    _add_common_artifact_argument(stress)
    _add_pytest_remainder(stress)
    stress.set_defaults(handler=run_stress)

    repeat = subparsers.add_parser(
        "repeat", help="repeat one suspected node ID in one and fresh processes"
    )
    repeat.add_argument("nodeid")
    repeat.add_argument(
        "--same-process", type=int, default=DEFAULT_SAME_PROCESS_REPEATS
    )
    repeat.add_argument("--fresh-runs", type=int, default=DEFAULT_FRESH_REPEATS)
    _add_common_artifact_argument(repeat)
    repeat.set_defaults(handler=run_repeat)

    parallel = subparsers.add_parser(
        "parallel", help="stress the suite with explicit xdist worker counts"
    )
    parallel.add_argument("--workers", type=int, action="append", default=[2, 4])
    _add_common_artifact_argument(parallel)
    _add_pytest_remainder(parallel)
    parallel.set_defaults(handler=run_parallel)

    quarantine = subparsers.add_parser(
        "quarantine", help="run confirmed quarantined tests repeatedly"
    )
    quarantine.add_argument("--runs", type=int, default=DEFAULT_QUARANTINE_RUNS)
    quarantine.add_argument("--seed", type=int, action="append", default=[])
    _add_common_artifact_argument(quarantine)
    _add_pytest_remainder(quarantine)
    quarantine.set_defaults(handler=run_quarantine)

    scan = subparsers.add_parser(
        "scan", help="write a static inventory of common flake-risk signals"
    )
    _add_common_artifact_argument(scan)
    scan.set_defaults(handler=run_scan)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ValueError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
