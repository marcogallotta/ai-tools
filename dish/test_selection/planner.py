"""Build deterministic test plans from changed paths and agent-selected escalations."""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .parallel import (
    EXPERIMENTAL_PARALLEL_TEST_FILES,
    experimental_parallel_command,
    experimental_parallel_eligible,
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
    "frontend check",
    "smoke",
    "SQLite database-boundary",
    "PGlite primary",
    "PGlite quarantine",
    "native PostgreSQL certification",
    "default mutation sample",
    "Stage A mutation sample",
    "source acceptance",
    "flake diagnostics",
    "ordinary full suite",
)

LANE_COMMANDS = {
    "frontend check": "npm --prefix frontend run check",
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
    commands: tuple[str, ...]
    experimental_parallel_eligible: bool
    experimental_commands: tuple[str, ...]
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
            "experimental_parallel_eligible": self.experimental_parallel_eligible,
            "experimental_commands": list(self.experimental_commands),
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
        if self.experimental_commands:
            lines.extend([
                "",
                "Experimental acceleration (non-authoritative; required serial commands still apply):",
            ])
            lines.extend(
                f"  {index}. {command}"
                for index, command in enumerate(self.experimental_commands, start=1)
            )
        elif self.experimental_parallel_eligible:
            lines.extend([
                "",
                "Experimental parallel candidate available for the focused tests.",
                "Rerun the planner with --experimental-workers N to emit a runnable optional command.",
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


def _ordered_lanes(lanes: set[str]) -> tuple[str, ...]:
    known = [lane for lane in LANE_ORDER if lane in lanes]
    unknown = sorted(lanes - set(LANE_ORDER) - FOCUSED_LANES)
    return tuple(known + unknown)


def _commands(focused_tests: set[str], lanes: set[str]) -> tuple[str, ...]:
    commands: list[str] = []
    focused = _focused_command(focused_tests)
    if focused:
        commands.append(focused)
    seen: set[str] = set(commands)
    for lane in _ordered_lanes(lanes):
        command = LANE_COMMANDS.get(lane)
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
    experimental_workers: int | None = None,
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
        lanes.update(row.default_lanes)
        if row.shared_infrastructure_scope in CONSUMER_SCOPE_USES_LANES:
            lanes.update(row.consumer_lanes)
        if row.escalation_predicates or row.conditional_escalations:
            reviews.append(
                ConditionalReview(
                    path=row.path,
                    predicates=row.escalation_predicates,
                    escalations=row.conditional_escalations,
                )
            )

    if integration_checkpoint:
        lanes.add("ordinary full suite")

    ordinary_focused = _ordinary_focused_tests(focused_tests)
    parallel_eligible = experimental_parallel_eligible(ordinary_focused)
    experimental_commands: tuple[str, ...] = ()
    if experimental_workers is not None:
        if experimental_workers < 1:
            raise PolicyError("--experimental-workers must be at least 1")
        if not parallel_eligible:
            unsupported = sorted(
                set(ordinary_focused) - set(EXPERIMENTAL_PARALLEL_TEST_FILES)
            )
            detail = ", ".join(unsupported) if unsupported else "no reviewed focused tests"
            raise PolicyError(
                "experimental parallel acceleration is unavailable for this focused selection: "
                + detail
            )
        experimental_commands = (
            experimental_parallel_command(ordinary_focused, workers=experimental_workers),
        )

    return TestPlan(
        changed_paths=normalized,
        ignored_paths=(),
        classes=tuple(sorted(classes)),
        traits=tuple(sorted(traits)),
        focused_tests=tuple(sorted(focused_tests)),
        lanes=tuple(sorted(lanes)),
        commands=_commands(focused_tests, lanes),
        experimental_parallel_eligible=parallel_eligible,
        experimental_commands=experimental_commands,
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
        raw.update(_git_lines(git_root, ["ls-files", "--others", "--exclude-standard"]))
    elif staged:
        raw = _git_lines(git_root, ["diff", "--cached", "--name-only", "--diff-filter=ACMRD"])
    else:
        raw = _git_lines(git_root, ["diff", "HEAD", "--name-only", "--diff-filter=ACMRD"])
        raw.update(_git_lines(git_root, ["ls-files", "--others", "--exclude-standard"]))

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
