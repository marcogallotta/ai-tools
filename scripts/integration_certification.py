#!/usr/bin/env python3
"""Reusable single-job Integration certification execution primitives.

This module deliberately does not decide which groups are required. Selector/planner
policy remains upstream authority. It consumes a validated execution spec, derives
conditional runtime requirements, executes selected groups in a deterministic order,
and writes one exact-candidate evidence document.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

EVIDENCE_SCHEMA = "dish-integration-certification-v2"
EXECUTION_SPEC_SCHEMA = "dish-certification-execution-spec-v2"
GROUP_ORDER = (
    "python-control-plane",
    "frontend-static",
    "native-postgresql",
    "browser-acceptance",
)
RESULTS = {
    "passed",
    "failed",
    "not_selected",
    "not_run_due_to_prior_failure",
}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class CertificationError(ValueError):
    """Raised for malformed or unsafe certification inputs."""


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    cwd: str | None = None


@dataclass(frozen=True)
class TargetSpec:
    target_id: str
    execution_boundary: str
    requirements: tuple[str, ...]
    commands: tuple[CommandSpec, ...]


@dataclass(frozen=True)
class ExecutionSpec:
    candidate_sha: str
    plan_digest: str
    targets: tuple[TargetSpec, ...]

    @property
    def required_groups(self) -> tuple[str, ...]:
        selected = {target.execution_boundary for target in self.targets}
        return tuple(group for group in GROUP_ORDER if group in selected)


CommandRunner = Callable[[CommandSpec, Path, Path], int]
Clock = Callable[[], float]


def _require_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CertificationError(f"{field} must be a non-empty string")
    return value


def load_execution_spec(path: Path) -> ExecutionSpec:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CertificationError(f"cannot read execution spec {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CertificationError("execution spec must be a JSON object")
    if raw.get("schema") != EXECUTION_SPEC_SCHEMA:
        raise CertificationError(
            f"execution spec schema must be {EXECUTION_SPEC_SCHEMA!r}"
        )

    candidate_sha = _require_string(raw.get("candidate_sha"), field="candidate_sha")
    if not _SHA_RE.fullmatch(candidate_sha):
        raise CertificationError("candidate_sha must be a lowercase 40-hex commit SHA")

    plan_digest = _require_string(raw.get("plan_digest"), field="plan_digest")
    if not _DIGEST_RE.fullmatch(plan_digest):
        raise CertificationError("plan_digest must be a lowercase SHA-256 hex digest")

    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, list):
        raise CertificationError("targets must be an array")
    targets: list[TargetSpec] = []
    seen_ids: set[str] = set()
    for target_index, raw_target in enumerate(raw_targets):
        if not isinstance(raw_target, dict):
            raise CertificationError(f"targets[{target_index}] must be an object")
        target_id = _require_string(raw_target.get("id"), field=f"targets[{target_index}].id")
        if target_id in seen_ids:
            raise CertificationError(f"duplicate target id {target_id}")
        seen_ids.add(target_id)
        group = _require_string(
            raw_target.get("execution_boundary"), field=f"targets[{target_index}].execution_boundary"
        )
        if group not in GROUP_ORDER:
            raise CertificationError(f"target {target_id} has unknown execution boundary {group}")
        raw_requirements = raw_target.get("requirements")
        if (
            not isinstance(raw_requirements, list)
            or any(not isinstance(value, str) or not value for value in raw_requirements)
            or len(raw_requirements) != len(set(raw_requirements))
        ):
            raise CertificationError(f"target {target_id} requirements must be unique strings")
        raw_commands = raw_target.get("commands")
        if not isinstance(raw_commands, list) or not raw_commands:
            raise CertificationError(f"selected target {target_id!r} must contain commands")
        commands: list[CommandSpec] = []
        for index, raw_command in enumerate(raw_commands):
            if not isinstance(raw_command, dict):
                raise CertificationError(f"{target_id}.commands[{index}] must be an object")
            name = _require_string(raw_command.get("name"), field=f"{target_id}.commands[{index}].name")
            argv = raw_command.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(arg, str) or not arg for arg in argv)
            ):
                raise CertificationError(
                    f"{target_id}.commands[{index}].argv must be a non-empty array of non-empty strings"
                )
            cwd = raw_command.get("cwd")
            if cwd is not None:
                if (
                    not isinstance(cwd, str)
                    or not cwd
                    or cwd.startswith(("/", "\\"))
                    or "\\" in cwd
                    or any(part in {"", ".", ".."} for part in cwd.split("/"))
                ):
                    raise CertificationError(
                        f"{target_id}.commands[{index}].cwd must be canonical repository-relative POSIX form"
                    )
            commands.append(CommandSpec(name=name, argv=tuple(argv), cwd=cwd))
        targets.append(TargetSpec(
            target_id=target_id,
            execution_boundary=group,
            requirements=tuple(raw_requirements),
            commands=tuple(commands),
        ))

    return ExecutionSpec(
        candidate_sha=candidate_sha,
        plan_digest=plan_digest,
        targets=tuple(targets),
    )


def setup_requirements(spec: ExecutionSpec) -> dict[str, bool]:
    requirements = {value for target in spec.targets for value in target.requirements}
    return {
        "python": "python" in requirements,
        "node": "node" in requirements,
        "postgresql": "postgresql" in requirements,
        "chromium": "chromium" in requirements,
        "flake": any(
            command.cwd == "dish" and command.argv and command.argv[0].startswith(".venv-flake/")
            for target in spec.targets for command in target.commands
        ),
    }


def _run_command(command: CommandSpec, repo_root: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    printable = shlex.join(command.argv)
    command_root = repo_root / command.cwd if command.cwd else repo_root
    print(f"[{command.name}] $ {printable}", flush=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[{command.name}] $ {printable}\n")
        log.flush()
        try:
            process = subprocess.Popen(
                command.argv,
                cwd=command_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            message = f"command launch failed: {exc}\n"
            print(message, end="", file=sys.stderr, flush=True)
            log.write(message)
            return 127

        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
        return process.wait()


def _elapsed(start: float, end: float) -> float:
    return round(max(0.0, end - start), 3)


def execute_certification(
    spec: ExecutionSpec,
    *,
    run_id: str,
    run_attempt: int,
    repo_root: Path,
    evidence_path: Path,
    command_runner: CommandRunner = _run_command,
    clock: Clock = time.monotonic,
) -> dict[str, object]:
    if not run_id.strip():
        raise CertificationError("run_id must be non-empty")
    if run_attempt < 1:
        raise CertificationError("run_attempt must be >= 1")

    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    targets_root = evidence_path.parent / "targets"
    total_start = clock()
    prior_failure = False
    target_results: dict[str, dict[str, object]] = {}

    ordered_targets = sorted(
        spec.targets, key=lambda target: (GROUP_ORDER.index(target.execution_boundary), target.target_id)
    )
    for target in ordered_targets:
        if prior_failure:
            target_results[target.target_id] = {
                "execution_boundary": target.execution_boundary,
                "result": "not_run_due_to_prior_failure",
                "elapsed_seconds": 0.0,
            }
            continue

        target_start = clock()
        result = "passed"
        for command_index, command in enumerate(target.commands, start=1):
            log_path = targets_root / _safe_name(target.target_id) / f"{command_index:02d}-{_safe_name(command.name)}.log"
            returncode = command_runner(command, repo_root, log_path)
            if returncode != 0:
                result = "failed"
                prior_failure = True
                break
        target_end = clock()
        target_results[target.target_id] = {
            "execution_boundary": target.execution_boundary,
            "result": result,
            "elapsed_seconds": _elapsed(target_start, target_end),
        }

    total_end = clock()
    outcome = "failed" if prior_failure else "passed"
    payload: dict[str, object] = {
        "schema": EVIDENCE_SCHEMA,
        "candidate_sha": spec.candidate_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "plan_digest": spec.plan_digest,
        "execution_order": [target.target_id for target in ordered_targets],
        "required_groups": list(spec.required_groups),
        "required_targets": [target.target_id for target in ordered_targets],
        "target_results": target_results,
        "elapsed_seconds": _elapsed(total_start, total_end),
        "outcome": outcome,
    }
    _validate_evidence(payload)
    evidence_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return normalized or "command"


def _validate_evidence(payload: Mapping[str, object]) -> None:
    required_targets = payload.get("required_targets")
    target_results = payload.get("target_results")
    if not isinstance(required_targets, list) or not isinstance(target_results, dict):
        raise CertificationError("evidence must contain target-level identity and results")
    if list(target_results) != required_targets:
        raise CertificationError("target results must follow deterministic required-target order")
    for target_id, entry in target_results.items():
        if not isinstance(entry, dict) or entry.get("result") not in RESULTS:
            raise CertificationError(f"invalid evidence result for {target_id}")
        elapsed = entry.get("elapsed_seconds")
        if not isinstance(elapsed, (int, float)) or elapsed < 0:
            raise CertificationError(f"invalid elapsed timing for {group}")


def _write_github_output(path: Path, requirements: Mapping[str, bool]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key in ("python", "node", "postgresql", "chromium", "flake"):
            handle.write(f"{key}={'true' if requirements[key] else 'false'}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="integration_certification")
    subparsers = parser.add_subparsers(dest="command", required=True)

    requirements = subparsers.add_parser(
        "requirements", help="derive conditional runtime setup from selected execution groups"
    )
    requirements.add_argument("--spec", type=Path, required=True)
    requirements.add_argument("--github-output", type=Path)

    run = subparsers.add_parser(
        "run", help="execute required groups in deterministic fail-fast Integration order"
    )
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    run.add_argument(
        "--run-attempt",
        type=int,
        default=int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
    )
    run.add_argument("--repo-root", type=Path, default=Path.cwd())
    run.add_argument(
        "--evidence",
        type=Path,
        default=Path(".test-artifacts/integration-certification/evidence.json"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        spec = load_execution_spec(args.spec)
        if args.command == "requirements":
            requirements = setup_requirements(spec)
            if args.github_output:
                _write_github_output(args.github_output, requirements)
            print(json.dumps(requirements, sort_keys=True))
            return 0

        payload = execute_certification(
            spec,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            repo_root=args.repo_root.resolve(),
            evidence_path=args.evidence,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0 if payload["outcome"] == "passed" else 1
    except CertificationError as exc:
        print(f"integration_certification: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
