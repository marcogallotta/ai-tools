"""Build deterministic test plans from changed paths and agent-selected escalations."""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .parallel import (
    parallel_safe_blockers,
    parallel_safe_command,
    parallel_safe_eligible,
)
from .model import (
    ALLOWED_LANES,
    CLASS_NAMES,
    POLICY_PATH,
    PolicyError,
    PolicyRow,
    load_policy,
    load_policy_text,
)

ROOT = Path(__file__).resolve().parents[1]
FOCUSED_LANES = {
    "exact changed test/module",
    "focused authority/identity",
    "focused ordinary",
    "focused postgresql runtime",
    "focused recovery/persistence",
    "focused release/import/dark-launch",
    "focused schema/model/migration",
}

LANE_ORDER = (
    "frontend static/tooling",
    "smoke",
    "SQLite database-boundary",
    "PGlite primary",
    "PGlite quarantine",
    "native PostgreSQL certification",
    "browser acceptance",
    "default mutation sample",
    "Stage A mutation sample",
    "source acceptance",
    "flake diagnostics",
    "ordinary full suite",
)

LANE_COMMANDS = {
    "frontend static/tooling": "npm --prefix frontend run check:static",
    "browser acceptance": "npm --prefix frontend run test:acceptance",
    "smoke": ".venv/bin/python -m pytest --smoke",
    "SQLite database-boundary": ".venv/bin/python -m pytest --database-boundary",
    "PGlite primary": (
        ".venv/bin/python scripts/dish-pg-pglite "
        "--output .test-artifacts/pglite/report.json"
    ),
    "PGlite quarantine": (
        ".venv/bin/python scripts/dish-pg-pglite "
        "--output .test-artifacts/pglite/report.json"
    ),
    "native PostgreSQL certification": (
        "DISH_TEST_POSTGRESQL_DSN='postgresql+psycopg://...' "
        ".venv/bin/python scripts/dish-pg-native-certification "
        "--output .test-artifacts/native-postgresql/report.json"
    ),
    "default mutation sample": ".venv/bin/python -m tests.mutation_runner",
    "Stage A mutation sample": ".venv/bin/python -m tests.mutation_runner --stage-a",
    "source acceptance": (
        ".venv/bin/python scripts/dish-pg-acceptance --skip-full "
        "--output .test-artifacts/stage-a-acceptance/report.json"
    ),
    "flake diagnostics": ".venv-flake/bin/python -m tests.flake_runner rerun-detect",
    "ordinary full suite": ".venv/bin/python -m pytest",
}

CONSUMER_SCOPE_USES_LANES = {"lane-local", "cross-lane", "generated-fixture", "global"}


@dataclass(frozen=True)
class ConditionalReview:
    path: str
    predicates: tuple[str, ...]
    escalations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "predicates": list(self.predicates),
            "escalations": list(self.escalations),
        }


@dataclass(frozen=True)
class TestPlan:
    changed_paths: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    classes: tuple[str, ...]
    traits: tuple[str, ...]
    focused_tests: tuple[str, ...]
    lanes: tuple[str, ...]
    native_postgresql_test_files: tuple[str, ...]
    native_postgresql_fully_bound: bool
    commands: tuple[str, ...]
    parallel_safe_eligible: bool
    parallel_workers: int | None
    parallel_acceleration_used: bool
    parallel_blockers: tuple[str, ...]
    conditional_reviews: tuple[ConditionalReview, ...]
    integration_checkpoint: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "format": "dish-test-plan-v1",
            "changed_paths": list(self.changed_paths),
            "ignored_paths": list(self.ignored_paths),
            "classes": list(self.classes),
            "traits": list(self.traits),
            "focused_tests": list(self.focused_tests),
            "lanes": list(self.lanes),
            "commands": list(self.commands),
            "parallel_safe_eligible": self.parallel_safe_eligible,
            "parallel_workers": self.parallel_workers,
            "parallel_acceleration_used": self.parallel_acceleration_used,
            "parallel_blockers": list(self.parallel_blockers),
            "conditional_reviews": [review.as_dict() for review in self.conditional_reviews],
            "integration_checkpoint": self.integration_checkpoint,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"

    def to_text(self) -> str:
        lines = ["Dish test plan", ""]
        lines.append("Changed paths:")
        lines.extend(f"  - {path}" for path in self.changed_paths)
        if self.ignored_paths:
            lines.append("Ignored paths outside Dish policy scope:")
            lines.extend(f"  - {path}" for path in self.ignored_paths)
        lines.append("Classes: " + (", ".join(self.classes) or "none"))
        lines.append("Traits: " + (", ".join(self.traits) or "none"))
        lines.append("")
        lines.append("Required commands:")
        lines.extend(f"  {index}. {command}" for index, command in enumerate(self.commands, start=1))
        if not self.commands:
            lines.append("  (none)")
        if self.parallel_acceleration_used:
            lines.extend([
                "",
                f"Parallel-safe focused execution selected with {self.parallel_workers} workers.",
                "Governed serial lanes, when present, remain serial.",
            ])
        elif self.parallel_safe_eligible:
            lines.extend([
                "",
                "Parallel-safe focused execution is available for the reviewed focused tests.",
                "Rerun the planner with --parallel-workers N to select that supported fast path.",
            ])
        elif self.parallel_workers is not None:
            lines.extend([
                "",
                "Parallel-safe focused execution was not selected; the focused command remains serial.",
                "Parallel blockers: " + ", ".join(self.parallel_blockers),
            ])
        if self.conditional_reviews:
            lines.extend(["", "Agent semantic review required:"])
            for review in self.conditional_reviews:
                lines.append(f"  - {review.path}")
                if review.predicates:
                    lines.append("    Evaluate: " + "; ".join(review.predicates))
                if review.escalations:
                    lines.append("    Escalate with: " + " | ".join(review.escalations))
        lines.extend(
            [
                "",
                "Report the chosen class/traits, commands run, results, and why any conditional lane was omitted.",
            ]
        )
        return "\n".join(lines) + "\n"


def _ordinary_focused_tests(tests: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                test
                for test in tests
                if not test.startswith("tests/postgresql/native/")
                and not test.startswith("tests/postgresql/pglite/")
                and not test.startswith("frontend/tests/")
            }
        )
    )


def _focused_command(tests: Iterable[str]) -> str | None:
    ordered = _ordinary_focused_tests(tests)
    if not ordered:
        return None
    return ".venv/bin/python -m pytest -q " + " ".join(shlex.quote(test) for test in ordered)


def _native_postgresql_test_files(tests: Iterable[str]) -> set[str]:
    return {
        test
        for test in tests
        if test.startswith("tests/postgresql/native/test_") and test.endswith(".py")
    }


def _row_native_postgresql_test_files(row: PolicyRow) -> set[str]:
    tests = set(row.direct_owner_tests) | set(row.critical_contract_tests)
    if row.kind == "test":
        tests.add(row.path)
    return _native_postgresql_test_files(tests)


def _native_postgresql_command(
    test_files: Iterable[str], *, fully_bound: bool
) -> str:
    command = LANE_COMMANDS["native PostgreSQL certification"]
    native_files = sorted(set(test_files))
    if not fully_bound or not native_files:
        return command
    return command + " " + " ".join(
        f"--test-file {shlex.quote(test_file)}" for test_file in native_files
    )


def _ordered_lanes(lanes: set[str]) -> tuple[str, ...]:
    known = [lane for lane in LANE_ORDER if lane in lanes]
    unknown = sorted(lanes - set(LANE_ORDER) - FOCUSED_LANES)
    return tuple(known + unknown)


def _commands(
    focused_tests: set[str],
    lanes: set[str],
    *,
    native_postgresql_test_files: set[str],
    native_postgresql_fully_bound: bool,
    focused_override: str | None = None,
) -> tuple[str, ...]:
    commands: list[str] = []
    focused = focused_override or _focused_command(focused_tests)
    if focused:
        commands.append(focused)
    seen: set[str] = set(commands)
    for lane in _ordered_lanes(lanes):
        command = (
            _native_postgresql_command(
                native_postgresql_test_files, fully_bound=native_postgresql_fully_bound
            )
            if lane == "native PostgreSQL certification"
            else LANE_COMMANDS.get(lane)
        )
        if command and command not in seen:
            commands.append(command)
            seen.add(command)
    return tuple(commands)


def build_plan(
    changed_paths: Iterable[str],
    *,
    policy_path: Path | None = None,
    fallback_policy: Mapping[str, PolicyRow] | None = None,
    add_lanes: Iterable[str] = (),
    integration_checkpoint: bool = False,
    parallel_workers: int | None = None,
) -> TestPlan:
    policy = load_policy(policy_path)
    fallback = fallback_policy or {}
    normalized = tuple(sorted(set(path.strip() for path in changed_paths if path.strip())))
    missing = [path for path in normalized if path not in policy and path not in fallback]
    if missing:
        raise PolicyError(
            "unclassified changed paths; classify them in test_selection/ownership.csv or pass only "
            "Dish-scoped paths: " + ", ".join(missing)
        )

    requested_lanes = set(add_lanes)
    invalid_lanes = requested_lanes - ALLOWED_LANES
    if invalid_lanes:
        raise PolicyError("unknown lane names: " + ", ".join(sorted(invalid_lanes)))

    rows: list[PolicyRow] = [policy.get(path) or fallback[path] for path in normalized]
    focused_tests: set[str] = set()
    lanes = set(requested_lanes)
    native_postgresql_test_files: set[str] = set()
    native_postgresql_fully_bound = "native PostgreSQL certification" not in requested_lanes
    classes: set[str] = set()
    traits: set[str] = set()
    reviews: list[ConditionalReview] = []

    for row in rows:
        classes.add(f"{row.primary_class}. {row.primary_class_name}")
        if row.kind == "test" and row.domain_class_for_tests != row.primary_class:
            classes.add(
                f"test domain {row.domain_class_for_tests}. "
                f"{CLASS_NAMES[row.domain_class_for_tests]}"
            )
        traits.update(row.traits)
        focused_tests.update(row.direct_owner_tests)
        focused_tests.update(row.critical_contract_tests)
        if row.kind == "test":
            focused_tests.add(row.path)
        row_lanes = set(row.default_lanes)
        if row.shared_infrastructure_scope in CONSUMER_SCOPE_USES_LANES:
            row_lanes.update(row.consumer_lanes)
        lanes.update(row_lanes)
        if "native PostgreSQL certification" in row_lanes:
            row_native_tests = _row_native_postgresql_test_files(row)
            if row_native_tests:
                native_postgresql_test_files.update(row_native_tests)
            else:
                native_postgresql_fully_bound = False
        if row.escalation_predicates or row.conditional_escalations:
            reviews.append(
                ConditionalReview(
                    path=row.path,
                    predicates=row.escalation_predicates,
                    escalations=row.conditional_escalations,
                )
            )

    ordinary_focused = _ordinary_focused_tests(focused_tests)
    parallel_eligible = parallel_safe_eligible(ordinary_focused)
    parallel_blockers = parallel_safe_blockers(ordinary_focused)
    parallel_used = False
    focused_override = None
    if parallel_workers is not None:
        if parallel_workers < 1:
            raise PolicyError("--parallel-workers must be at least 1")
        if parallel_eligible:
            focused_override = parallel_safe_command(ordinary_focused, workers=parallel_workers)
            parallel_used = True

    return TestPlan(
        changed_paths=normalized,
        ignored_paths=(),
        classes=tuple(sorted(classes)),
        traits=tuple(sorted(traits)),
        focused_tests=tuple(sorted(focused_tests)),
        lanes=tuple(sorted(lanes)),
        native_postgresql_test_files=tuple(sorted(native_postgresql_test_files)),
        native_postgresql_fully_bound=native_postgresql_fully_bound,
        commands=_commands(
            focused_tests,
            lanes,
            native_postgresql_test_files=native_postgresql_test_files,
            native_postgresql_fully_bound=native_postgresql_fully_bound,
            focused_override=focused_override,
        ),
        parallel_safe_eligible=parallel_eligible,
        parallel_workers=parallel_workers,
        parallel_acceleration_used=parallel_used,
        parallel_blockers=parallel_blockers,
        conditional_reviews=tuple(reviews),
        integration_checkpoint=integration_checkpoint,
    )


def _git_root(cwd: Path) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise PolicyError("cannot discover Git root; use one or more --path arguments")
    return Path(completed.stdout.strip()).resolve()


def _git_lines(git_root: Path, args: Sequence[str]) -> set[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=git_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise PolicyError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}



def load_git_policy(*, repo_root: Path = ROOT, ref: str = "HEAD") -> dict[str, PolicyRow]:
    """Load the ownership map at *ref* so deleted paths retain their prior classification."""
    git_root = _git_root(repo_root)
    try:
        relative_policy = POLICY_PATH.resolve().relative_to(git_root).as_posix()
    except ValueError as exc:
        raise PolicyError("test-selection policy is outside the Git repository") from exc
    completed = subprocess.run(
        ["git", "show", f"{ref}:{relative_policy}"],
        cwd=git_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        # The first commit introducing the selector has no historical map. New/current paths still
        # classify normally; only a deletion from that same initial commit cannot use fallback.
        return {}
    return load_policy_text(completed.stdout, source=f"{ref}:{relative_policy}")


def discover_git_paths(
    *,
    repo_root: Path = ROOT,
    base: str | None = None,
    staged: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    git_root = _git_root(repo_root)
    if base and staged:
        raise PolicyError("--base and --staged are mutually exclusive")
    if base:
        raw = _git_lines(git_root, ["diff", "--name-only", "--diff-filter=ACMRD", base])
    elif staged:
        raw = _git_lines(git_root, ["diff", "--cached", "--name-only", "--diff-filter=ACMRD"])
    else:
        raw = _git_lines(git_root, ["diff", "HEAD", "--name-only", "--diff-filter=ACMRD"])

    scoped: set[str] = set()
    ignored: set[str] = set()
    for value in raw:
        absolute = (git_root / value).resolve(strict=False)
        try:
            scoped.add(absolute.relative_to(repo_root).as_posix())
            continue
        except ValueError:
            pass
        if absolute.parent == repo_root.parent and absolute.name in {"AGENTS.md", "CLAUDE.md", "README.md"}:
            scoped.add(f"../{absolute.name}")
        else:
            ignored.add(value)
    return tuple(sorted(scoped)), tuple(sorted(ignored))


def normalize_explicit_paths(paths: Iterable[str], *, repo_root: Path = ROOT) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in paths:
        value = raw.strip()
        if not value:
            continue
        if value.startswith("../"):
            normalized.add(Path(value).as_posix())
            continue
        path = Path(value)
        absolute = path.resolve(strict=False) if path.is_absolute() else (Path.cwd() / path).resolve(strict=False)
        try:
            normalized.add(absolute.relative_to(repo_root).as_posix())
            continue
        except ValueError:
            pass
        if absolute.parent == repo_root.parent and absolute.name in {"AGENTS.md", "CLAUDE.md", "README.md"}:
            normalized.add(f"../{absolute.name}")
            continue
        # Accept already-normalized repository-relative values even when the caller is outside dish/.
        if (repo_root / value).exists() or not path.is_absolute():
            normalized.add(Path(value).as_posix())
            continue
        raise PolicyError(f"path is outside Dish policy scope: {raw}")
    return tuple(sorted(normalized))
