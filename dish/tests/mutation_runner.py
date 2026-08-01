"""Run a small, deterministic mutation sample against launch-critical invariants."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from tests.mutation_cases import CASES, MutationCase


ROOT = Path(__file__).resolve().parents[1]
COPY_ENTRIES = ("dish_tool", "dish_service", "tests", "pytest.ini")
CASE_TIMEOUT_SECONDS = 120


def apply_mutation(path: Path, case: MutationCase) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(case.before)
    if count != 1:
        raise RuntimeError(
            f"{case.mutation_id}: expected one source match in {case.target}, found {count}"
        )
    path.write_text(text.replace(case.before, case.after, 1), encoding="utf-8")


def classify_pytest_exit(returncode: int) -> str:
    if returncode == 0:
        return "survived"
    if returncode == 1:
        return "killed"
    return "infrastructure_error"


def _copy_workspace(destination: Path) -> None:
    for entry in COPY_ENTRIES:
        source = ROOT / entry
        target = destination / entry
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".test-artifacts"),
            )
        else:
            shutil.copy2(source, target)


def run_case(case: MutationCase) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"dish-mutant-{case.mutation_id}-") as raw:
        workspace = Path(raw)
        _copy_workspace(workspace)
        apply_mutation(workspace / case.target, case)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *case.tests,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                    text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=CASE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            return {
                **asdict(case),
                "command": command,
                "returncode": None,
                "outcome": "infrastructure_error",
                "output": f"mutation test timed out after {CASE_TIMEOUT_SECONDS}s\n{output}",
            }
        return {
            **asdict(case),
            "command": command,
            "returncode": completed.returncode,
            "outcome": classify_pytest_exit(completed.returncode),
            "output": completed.stdout,
        }


def run(cases=CASES, *, artifacts: Path) -> int:
    artifacts.mkdir(parents=True, exist_ok=True)
    results = [run_case(case) for case in cases]
    summary = {
        "total": len(results),
        "killed": sum(item["outcome"] == "killed" for item in results),
        "survived": sum(item["outcome"] == "survived" for item in results),
        "infrastructure_errors": sum(
            item["outcome"] == "infrastructure_error" for item in results
        ),
        "results": results,
    }
    (artifacts / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# Dish mutation sample",
        "",
        f"- killed: {summary['killed']}/{summary['total']}",
        f"- survived: {summary['survived']}",
        f"- infrastructure errors: {summary['infrastructure_errors']}",
        "",
    ]
    for item in results:
        lines.extend(
            [
                f"## {item['mutation_id']}: {item['outcome']}",
                "",
                f"Invariant: {item['invariant']}",
                "",
                "```text",
                item["output"].rstrip(),
                "```",
                "",
            ]
        )
    (artifacts / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return 0 if summary["survived"] == 0 and summary["infrastructure_errors"] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifacts",
        default=".test-artifacts/mutation-sample",
        help="directory for JSON and Markdown mutation results",
    )
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        for case in CASES:
            print(f"{case.mutation_id}: {case.invariant}")
        return 0
    return run(artifacts=Path(args.artifacts))


if __name__ == "__main__":
    raise SystemExit(main())
