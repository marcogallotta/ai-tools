from __future__ import annotations

import copy
from pathlib import Path

from tests.support.verification import TASK


def review_and_inspect(app, operation_id: str, *, run_id: str = "dish-020-review"):
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id=run_id,
        independence_attestation="independent",
    )
    assert review["ok"]
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    return review, inspected


def small_candidate(path: Path) -> Path:
    candidate = path / "small-correction.txt"
    candidate.write_text(
        TASK.replace("1. Cook it.", "1. Cook it gently."), encoding="utf-8"
    )
    return candidate


def without_replay_marker(result):
    normalized = copy.deepcopy(result)
    normalized.get("data", {}).pop("request_replayed", None)
    return normalized
