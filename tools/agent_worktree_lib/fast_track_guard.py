from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .common import GitRunner, fail, require_task_gid
from .commit import _claimed_state as _commit_claimed_state
from .operations import remote_ref_sha, resolve_repository_from_state, verify_owned_worktree
from .fast_track_auth import FastTrackAuthorization, _fallback, _live_authorization, high_consequence_reason, assert_fast_track_worktree

def _worktree_changed_paths(runner: GitRunner, worktree: Path) -> set[str]:
    tracked = runner.run(
        worktree,
        "diff",
        "HEAD",
        "--name-only",
        "--no-ext-diff",
        "--no-textconv",
        "-z",
    ).stdout
    untracked = runner.run(
        worktree,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    ).stdout
    return {item for item in (tracked + untracked).split("\0") if item}


def _commit_changed_paths(runner: GitRunner, worktree: Path, base: str, head: str) -> set[str]:
    raw = runner.run(
        worktree,
        "diff",
        "--name-only",
        "--no-ext-diff",
        "--no-textconv",
        "-z",
        f"{base}..{head}",
    ).stdout
    return {item for item in raw.split("\0") if item}


def _require_bounded(actual: set[str], auth: FastTrackAuthorization, *, label: str) -> None:
    authorized = set(auth.paths)
    outside = sorted(actual - authorized)
    if outside:
        _fallback(f"{label} escaped the authorized path set: {', '.join(outside)}")
    if auth.mode == "TRIVIAL":
        for path in sorted(actual):
            reason = high_consequence_reason(path)
            if reason:
                _fallback(reason)


def _require_fresh_base(runner: GitRunner, repo: Any, auth: FastTrackAuthorization) -> None:
    current = remote_ref_sha(runner, repo, auth.base_ref)
    assert current is not None
    if current != auth.base_head:
        _fallback(f"authorized base {auth.base_head} is stale; current {auth.base_ref} is {current}")


def _validated_context(args: argparse.Namespace, runner: GitRunner) -> tuple[dict[str, Any], Any, Any, FastTrackAuthorization]:
    task_gid = require_task_gid(args.task)
    state = _commit_claimed_state(task_gid, runner)
    repo = resolve_repository_from_state(runner, state)
    identity = verify_owned_worktree(runner, repo, state)
    assert_fast_track_worktree(identity, repo)
    auth = _live_authorization(task_gid, getattr(args, "authorization_story", None), state)
    _require_fresh_base(runner, repo, auth)
    return state, repo, identity, auth
