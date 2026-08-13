"""Deterministic cheap validation that must pass before governed broad lanes."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .model import POLICY_PATH, PolicyError, PolicyRow, load_policy
from .parallel import (
    PARALLEL_SAFE_TEST_FILES,
    QUALIFICATION_MANIFEST,
    SHARED_QUALIFICATION_INPUTS,
    parallel_safe_qualification_reason,
)
from .planner import TestPlan, build_plan
from .validator import validate_policy

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_CONTRACT_TRAIT = "preflight-contract"
GLOBAL_TEST_PREFLIGHT_TRAIT = "global-test-preflight-contract"
GOVERNED_BASELINE_TRAIT = "governed-baseline"


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "reason": self.reason}


@dataclass(frozen=True)
class PreflightResult:
    changed_paths: tuple[str, ...]
    selected_downstream_lanes: tuple[str, ...]
    checks: tuple[PreflightCheck, ...]
    contract_tests: tuple[str, ...] = ()
    contract_output: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "format": "dish-test-preflight-v1",
            "passed": self.passed,
            "changed_paths": list(self.changed_paths),
            "selected_downstream_lanes": list(self.selected_downstream_lanes),
            "contract_tests": list(self.contract_tests),
            "checks": [check.as_dict() for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"

    def to_text(self) -> str:
        lines = [f"Dish test preflight: {'PASS' if self.passed else 'FAIL'}", "", "Checks:"]
        for check in self.checks:
            lines.append(
                f"  {'PASS' if check.passed else 'FAIL'} {check.name}: {check.reason}"
            )
        lines.extend(["", "Selected downstream lanes (not run by preflight):"])
        if self.selected_downstream_lanes:
            lines.extend(f"  - {lane}" for lane in self.selected_downstream_lanes)
        else:
            lines.append("  (none)")
        if self.contract_output.strip():
            lines.extend(["", "Contract test output:", self.contract_output.rstrip()])
        return "\n".join(lines) + "\n"


def parallel_safe_qualification_targets_for_changes(
    changed_paths: Iterable[str], *, root: Path = ROOT
) -> tuple[str, ...]:
    """Return reviewed files whose qualification can be invalidated by this changed surface."""
    changed = set(path.strip() for path in changed_paths if path.strip())
    inventory = tuple(PARALLEL_SAFE_TEST_FILES)
    shared_changed = any(
        path == shared or path.startswith(f"{shared}/")
        for path in changed
        for shared in SHARED_QUALIFICATION_INPUTS
    )
    if QUALIFICATION_MANIFEST.as_posix() in changed or shared_changed:
        return inventory
    return tuple(path for path in inventory if path in changed)


def _merged_policy(
    policy: Mapping[str, PolicyRow], fallback: Mapping[str, PolicyRow] | None
) -> dict[str, PolicyRow]:
    merged = dict(fallback or {})
    merged.update(policy)
    return merged


def _is_test_surface(row: PolicyRow | None) -> bool:
    return row is not None and row.kind in {"test", "shared-test-infrastructure"}


def _preflight_contract(path: str, policy: Mapping[str, PolicyRow]) -> bool:
    row = policy.get(path)
    return row is not None and PREFLIGHT_CONTRACT_TRAIT in row.traits


def _governed_baseline_sources(path: Path) -> set[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()
    sources = value.get("governing_source_sha256") if isinstance(value, dict) else None
    return set(sources) if isinstance(sources, dict) else set()


def select_preflight_contract_tests(
    *,
    changed_paths: Iterable[str],
    plan: TestPlan,
    policy: Mapping[str, PolicyRow],
    repo_root: Path = ROOT,
) -> tuple[str, ...]:
    """Select only ownership-declared cheap contracts relevant to this changed surface."""
    changed = set(changed_paths)
    selected = {path for path in plan.focused_tests if _preflight_contract(path, policy)}

    if any(_is_test_surface(policy.get(path)) for path in changed):
        selected.update(
            row.path
            for row in policy.values()
            if row.path.startswith("tests/")
            and row.path.endswith(".py")
            and GLOBAL_TEST_PREFLIGHT_TRAIT in row.traits
        )

    for baseline in policy.values():
        if GOVERNED_BASELINE_TRAIT not in baseline.traits:
            continue
        sources = _governed_baseline_sources(repo_root / baseline.path)
        if baseline.path not in changed and not (changed & sources):
            continue
        for test_path in (*baseline.direct_owner_tests, *baseline.critical_contract_tests):
            if _preflight_contract(test_path, policy):
                selected.add(test_path)

    return tuple(sorted(selected))


def run_preflight(
    changed_paths: Iterable[str],
    *,
    policy_path: Path | None = None,
    fallback_policy: Mapping[str, PolicyRow] | None = None,
    add_lanes: Iterable[str] = (),
    integration_checkpoint: bool = False,
    parallel_workers: int | None = None,
    repo_root: Path = ROOT,
) -> PreflightResult:
    """Run deterministic structural/contract checks without executing downstream lanes."""
    repo_root = repo_root.resolve()
    normalized = tuple(sorted(set(path.strip() for path in changed_paths if path.strip())))
    checks: list[PreflightCheck] = []

    validation = validate_policy(
        repo_root=repo_root,
        parent_root=repo_root.parent,
        policy_path=policy_path or POLICY_PATH,
    )
    if not validation.ok:
        checks.append(
            PreflightCheck(
                "ownership-map",
                False,
                f"{validation.summary()}; " + " | ".join(validation.errors),
            )
        )
        return PreflightResult(normalized, (), tuple(checks))
    checks.append(PreflightCheck("ownership-map", True, validation.summary()))

    try:
        plan = build_plan(
            normalized,
            policy_path=policy_path,
            fallback_policy=fallback_policy,
            add_lanes=add_lanes,
            integration_checkpoint=integration_checkpoint,
            parallel_workers=parallel_workers,
        )
    except PolicyError as exc:
        checks.append(PreflightCheck("test-selection", False, str(exc)))
        return PreflightResult(normalized, (), tuple(checks))
    checks.append(
        PreflightCheck(
            "test-selection",
            True,
            f"selected {len(plan.lanes)} downstream lane(s); none executed",
        )
    )

    policy = _merged_policy(load_policy(policy_path), fallback_policy)
    try:
        qualification_targets = parallel_safe_qualification_targets_for_changes(
            normalized, root=repo_root
        )
        qualification_failures = tuple(
            (path, reason)
            for path in qualification_targets
            if (reason := parallel_safe_qualification_reason(path, root=repo_root)) is not None
        )
    except PolicyError as exc:
        checks.append(PreflightCheck("parallel-safe-qualification", False, str(exc)))
        return PreflightResult(normalized, plan.lanes, tuple(checks))
    if qualification_failures:
        checks.append(
            PreflightCheck(
                "parallel-safe-qualification",
                False,
                "; ".join(f"{path}: {reason}" for path, reason in qualification_failures),
            )
        )
        return PreflightResult(normalized, plan.lanes, tuple(checks))
    checks.append(
        PreflightCheck(
            "parallel-safe-qualification",
            True,
            (
                f"{len(qualification_targets)} relevant reviewed file(s) remain qualified"
                if qualification_targets
                else "not applicable to changed surface"
            ),
        )
    )

    contract_tests = select_preflight_contract_tests(
        changed_paths=normalized,
        plan=plan,
        policy=policy,
        repo_root=repo_root,
    )
    if not contract_tests:
        checks.append(
            PreflightCheck(
                "cheap-contracts",
                True,
                "no ownership-declared cheap contract tests selected",
            )
        )
        return PreflightResult(normalized, plan.lanes, tuple(checks))

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *contract_tests],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    checks.append(
        PreflightCheck(
            "cheap-contracts",
            completed.returncode == 0,
            (
                f"{len(contract_tests)} ownership-declared contract test(s) passed"
                if completed.returncode == 0
                else f"pytest exit code {completed.returncode} for {len(contract_tests)} contract test(s)"
            ),
        )
    )
    return PreflightResult(
        normalized,
        plan.lanes,
        tuple(checks),
        contract_tests,
        completed.stdout,
    )
