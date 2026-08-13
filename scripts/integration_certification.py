#!/usr/bin/env python3
"""Execute selected Integration certification groups from the landed repository planner contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

PLAN_FORMAT = "repository-certification-plan-v1"
EVIDENCE_FORMAT = "dish-integration-certification-v1"
COMMAND_MAP_FORMAT = "dish-certification-command-map-v1"
GROUP_ORDER = (
    "python-control-plane",
    "frontend-static",
    "native-postgresql",
    "browser-acceptance",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CertificationError(ValueError):
    """Raised when plan/command inputs cannot be executed safely."""


@dataclass(frozen=True)
class Command:
    name: str
    argv: tuple[str, ...]


def _load_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CertificationError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CertificationError(f"{label} must be a JSON object")
    return value


def _canonical_plan_bytes(plan: Mapping[str, object]) -> bytes:
    return (json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def plan_digest(plan: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_plan_bytes(plan)).hexdigest()


def load_plan(path: Path) -> dict[str, object]:
    plan = _load_json(path, label="certification plan")
    if plan.get("format") != PLAN_FORMAT:
        raise CertificationError(f"certification plan format must be {PLAN_FORMAT!r}")
    identity = plan.get("identity")
    if not isinstance(identity, dict):
        raise CertificationError("certification plan identity must be an object")
    candidate = identity.get("candidate_sha")
    if not isinstance(candidate, str) or not SHA_RE.fullmatch(candidate):
        raise CertificationError("certification plan candidate_sha must be exact lowercase 40-hex")
    selected = plan.get("selected_groups")
    if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
        raise CertificationError("certification plan selected_groups must be a string array")
    unknown = sorted(set(selected) - set(GROUP_ORDER))
    if unknown:
        raise CertificationError("certification plan selected unknown groups: " + ", ".join(unknown))
    expected_order = [group for group in GROUP_ORDER if group in selected]
    if selected != expected_order:
        raise CertificationError("certification plan selected_groups must follow planner execution-group order")
    return plan


def load_commands(path: Path, *, selected_groups: Sequence[str]) -> dict[str, tuple[Command, ...]]:
    raw = _load_json(path, label="certification command map")
    if raw.get("format") != COMMAND_MAP_FORMAT:
        raise CertificationError(f"certification command map format must be {COMMAND_MAP_FORMAT!r}")
    groups = raw.get("commands")
    if not isinstance(groups, dict):
        raise CertificationError("certification command map commands must be an object")
    unknown = sorted(set(groups) - set(GROUP_ORDER))
    if unknown:
        raise CertificationError("unknown execution groups: " + ", ".join(unknown))
    unselected = sorted(set(groups) - set(selected_groups))
    if unselected:
        raise CertificationError("command map supplies unselected groups: " + ", ".join(unselected))
    missing = [group for group in selected_groups if group not in groups]
    if missing:
        raise CertificationError("selected groups missing commands: " + ", ".join(missing))

    result: dict[str, tuple[Command, ...]] = {}
    for group in selected_groups:
        items = groups[group]
        if not isinstance(items, list) or not items:
            raise CertificationError(f"{group} commands must be a non-empty array")
        parsed: list[Command] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise CertificationError(f"{group} command {index} must be an object")
            name = item.get("name")
            argv = item.get("argv")
            if not isinstance(name, str) or not name.strip():
                raise CertificationError(f"{group} command {index} name must be non-empty")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(arg, str) and arg for arg in argv)
            ):
                raise CertificationError(f"{group} command {index} argv must be a non-empty string array")
            parsed.append(Command(name=name.strip(), argv=tuple(argv)))
        result[group] = tuple(parsed)
    return result


def setup_requirements(plan: Mapping[str, object]) -> dict[str, bool]:
    selected = set(plan["selected_groups"])  # validated by load_plan
    return {
        "python": bool(selected & {"python-control-plane", "native-postgresql", "browser-acceptance"}),
        "node": bool(selected & {"frontend-static", "browser-acceptance"}),
        "postgresql": "native-postgresql" in selected,
        "chromium": "browser-acceptance" in selected,
    }


def _safe_name(value: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return rendered or "command"


def _default_runner(command: Command, repo_root: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as stream:
        completed = subprocess.run(
            list(command.argv),
            cwd=repo_root,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return int(completed.returncode)


def execute_certification(
    plan: Mapping[str, object],
    commands: Mapping[str, tuple[Command, ...]],
    *,
    run_id: str,
    run_attempt: int,
    repo_root: Path,
    evidence_path: Path,
    command_runner: Callable[[Command, Path, Path], int] = _default_runner,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    if not run_id:
        raise CertificationError("run_id must be non-empty")
    if not isinstance(run_attempt, int) or isinstance(run_attempt, bool) or run_attempt < 1:
        raise CertificationError("run_attempt must be a positive integer")

    selected = tuple(plan["selected_groups"])
    candidate = plan["identity"]["candidate_sha"]  # type: ignore[index]
    results: dict[str, dict[str, object]] = {}
    failed = False
    overall_start = clock()

    for group in GROUP_ORDER:
        if group not in selected:
            results[group] = {"result": "not_selected", "elapsed_seconds": 0.0}
            continue
        if failed:
            results[group] = {"result": "not_run_due_to_prior_failure", "elapsed_seconds": 0.0}
            continue

        group_start = clock()
        group_failed = False
        for index, command in enumerate(commands[group], start=1):
            log_path = (
                evidence_path.parent
                / "logs"
                / group
                / f"{index:02d}-{_safe_name(command.name)}.log"
            )
            if command_runner(command, repo_root, log_path) != 0:
                group_failed = True
                break
        elapsed = round(max(0.0, clock() - group_start), 6)
        if group_failed:
            results[group] = {"result": "failed", "elapsed_seconds": elapsed}
            failed = True
        else:
            results[group] = {"result": "passed", "elapsed_seconds": elapsed}

    total_elapsed = round(max(0.0, clock() - overall_start), 6)
    payload: dict[str, object] = {
        "format": EVIDENCE_FORMAT,
        "candidate_sha": candidate,
        "run_id": str(run_id),
        "run_attempt": run_attempt,
        "plan_digest": plan_digest(plan),
        "required_groups": list(selected),
        "execution_order": list(GROUP_ORDER),
        "group_results": results,
        "elapsed_seconds": total_elapsed,
        "outcome": "failed" if failed else "passed",
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _write_github_output(path: Path, values: Mapping[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for key in ("python", "node", "postgresql", "chromium"):
            stream.write(f"{key}={'true' if values[key] else 'false'}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="integration_certification.py")
    sub = parser.add_subparsers(dest="command", required=True)

    requirements = sub.add_parser("requirements")
    requirements.add_argument("--plan", type=Path, required=True)
    requirements.add_argument("--github-output", type=Path)

    run = sub.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--commands", type=Path, required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--run-attempt", type=int, required=True)
    run.add_argument("--repo-root", type=Path, required=True)
    run.add_argument("--evidence", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = load_plan(args.plan)
        if args.command == "requirements":
            values = setup_requirements(plan)
            if args.github_output:
                _write_github_output(args.github_output, values)
            else:
                sys.stdout.write(json.dumps(values, sort_keys=True) + "\n")
            return 0

        commands = load_commands(args.commands, selected_groups=plan["selected_groups"])
        payload = execute_certification(
            plan,
            commands,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            repo_root=args.repo_root.resolve(),
            evidence_path=args.evidence,
        )
        return 0 if payload["outcome"] == "passed" else 1
    except CertificationError as exc:
        print(f"integration_certification: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
