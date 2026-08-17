from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess
from typing import Iterable


class RecoveryError(RuntimeError):
    """The requested mechanical recovery cannot be proved safe."""


@dataclass(frozen=True)
class RecoveryPlan:
    schema: str
    status: str
    repository_path: str
    landed_sha: str
    current_main_sha: str
    current_main_ref: str
    landing_kind: str | None
    landed_parents: tuple[str, ...]
    mainline_parent: str | None
    changed_paths: tuple[str, ...]
    later_touching_paths: tuple[str, ...]
    conflict_paths: tuple[str, ...]
    inverse_tree_sha: str | None
    reason: str | None
    source_reversal_scope: str
    runtime_effects_reversed: bool
    known_residual_effects: tuple[str, ...]
    next_action: str

    def json(self) -> dict[str, object]:
        value = asdict(self)
        for key in (
            "landed_parents",
            "changed_paths",
            "later_touching_paths",
            "conflict_paths",
            "known_residual_effects",
        ):
            value[key] = list(value[key])
        return value


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RecoveryError(f"git {' '.join(args)} failed: {detail}")
    return completed


def sha(repo: Path, value: str) -> str:
    resolved = git(repo, "rev-parse", "--verify", f"{value}^{{commit}}").stdout.strip()
    if len(resolved) != 40:
        raise RecoveryError(f"{value!r} did not resolve to an exact commit SHA")
    return resolved


def inverse_args(landing_kind: str, landed_sha: str) -> list[str]:
    if landing_kind == "one-parent":
        return ["revert", "--no-commit", landed_sha]
    if landing_kind == "true-merge":
        return ["revert", "--no-commit", "-m", "1", landed_sha]
    raise RecoveryError(f"unsupported landing kind {landing_kind!r}")


def residual_effects(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip().lower() for value in values if value.strip()}))
