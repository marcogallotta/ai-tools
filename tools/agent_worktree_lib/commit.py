from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from .common import GitRunner, fail, now_utc, path_is_within, require_full_sha, require_task_gid
from .operations import (
    ensure_commit_object,
    owner_agent_id,
    payload_from_state,
    remote_ref_sha,
    resolve_repository_from_state,
    verify_owned_worktree,
)
from .ownership import require_active_claim
from .state import TaskLock, atomic_write_json, load_task_state, state_path

CARPET_PATHS = {".", "-A", "-u", "--all"}


def _claimed_state(task_gid: str) -> dict[str, Any]:
    state = load_task_state(task_gid)
    claimed_agent = require_active_claim(task_gid, str(state["branch"]), owner_agent_id(state))["agent_id"]
    if owner_agent_id(state) is None:
        fail(
            "OWNER_MISMATCH",
            f"task record has no concrete owner; claimed agent {claimed_agent!r} must resume --takeover first",
        )
    return state


def _normalize_explicit_paths(worktree: Path, raw_paths: list[str]) -> list[str]:
    if not raw_paths:
        fail("EXPLICIT_PATHS_REQUIRED", "commit requires at least one explicit repository path")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        if raw in CARPET_PATHS:
            fail("CARPET_STAGING_REFUSED", f"carpet staging path/flag is not allowed: {raw!r}")
        if not raw or raw.startswith(":"):
            fail("INVALID_COMMIT_PATH", f"commit path must be a literal repository path: {raw!r}")
        candidate = Path(raw) if Path(raw).is_absolute() else worktree / raw
        absolute = Path(os.path.abspath(candidate))
        if absolute == worktree or not path_is_within(absolute, worktree):
            fail("COMMIT_PATH_ESCAPE", f"commit path escapes or names the repository root: {raw!r}")
        relative = absolute.relative_to(worktree).as_posix()
        if relative not in seen:
            normalized.append(relative)
            seen.add(relative)
    return normalized


def _staged_paths(runner: GitRunner, worktree: Path) -> set[str]:
    result = runner.run(
        worktree,
        "diff",
        "--cached",
        "--name-only",
        "--no-ext-diff",
        "--no-textconv",
        "-z",
    )
    return {path for path in result.stdout.split("\0") if path}


def _path_scopes(worktree: Path, explicit: list[str]) -> list[tuple[str, bool]]:
    scopes: list[tuple[str, bool]] = []
    for path in explicit:
        candidate = worktree / path
        scopes.append((path, candidate.is_dir()))
    return scopes


def _covered(path: str, scopes: list[tuple[str, bool]]) -> bool:
    return any(path == scope or (is_dir and path.startswith(scope.rstrip("/") + "/")) for scope, is_dir in scopes)


def _verify_staged_subset(staged: set[str], scopes: list[tuple[str, bool]]) -> None:
    outside = sorted(path for path in staged if not _covered(path, scopes))
    if outside:
        fail(
            "STAGED_PATH_OUTSIDE_EXPLICIT_SET",
            "index already contains staged path(s) outside the explicit commit paths: " + ", ".join(outside),
        )


def _require_current_merge_target(
    runner: GitRunner, repo: Any, state: dict[str, Any], expected_head: str
) -> str:
    expected_head = require_full_sha(expected_head, "--merge-target-head")
    base_ref = str(state["base_ref"])
    current = remote_ref_sha(runner, repo, base_ref)
    assert current is not None
    if current != expected_head:
        fail(
            "STALE_MERGE_TARGET",
            f"exact remote {base_ref} head is {current}, not the expected reconciliation target {expected_head}; "
            "re-verify and retry with the current head",
        )
    return current


def _resolve_merge_target(
    runner: GitRunner, repo: Any, state: dict[str, Any], expected_head: str
) -> str:
    current = _require_current_merge_target(runner, repo, state, expected_head)
    ensure_commit_object(runner, repo, current)
    return current


def _show(runner: GitRunner, worktree: Path, spec: str) -> str | None:
    result = runner.run(worktree, "show", spec, check=False)
    return result.stdout if result.returncode == 0 else None


def _parse_version(text: str | None, source: str) -> dict[str, str] | None:
    if text is None:
        return None
    values: dict[str, str] = {}
    allowed = {"PROTOCOL_VERSION", "SCHEMA_VERSION"}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        if raw.count("=") != 1:
            fail("COMMIT_GUARD_REFUSED", f"{source} line {line_number} must be KEY=VALUE")
        key, value = raw.split("=", 1)
        if key not in allowed:
            fail("COMMIT_GUARD_REFUSED", f"{source} has unknown key {key!r}")
        if key in values:
            fail("COMMIT_GUARD_REFUSED", f"{source} contains duplicate key {key}")
        if not value or value != value.strip():
            fail("COMMIT_GUARD_REFUSED", f"{source} has invalid value for {key}")
        values[key] = value
    missing = sorted(allowed - set(values))
    if missing:
        fail("COMMIT_GUARD_REFUSED", f"{source} is missing required key(s): {', '.join(missing)}")
    return values


def _schema_asset_has_structural_change(runner: GitRunner, worktree: Path, path: str) -> bool:
    status = runner.run(
        worktree,
        "diff",
        "--cached",
        "--name-status",
        "--find-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--",
        path,
    ).stdout.splitlines()
    if not status or any(not line.startswith("M\t") for line in status):
        return True

    diff = runner.run(
        worktree,
        "diff",
        "--cached",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--",
        path,
    ).stdout
    for line in diff.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        changed = line[1:].strip()
        if changed.startswith('"protocol_version"'):
            continue
        return True
    return False


def _check_dish_version_guard(runner: GitRunner, worktree: Path) -> None:
    # Mirror tools/git-commit's staged DISH_VERSION invariant without importing
    # or executing a mutable helper from the candidate worktree.
    staged_raw = runner.run(
        worktree,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMRTD",
        "--no-ext-diff",
        "--no-textconv",
        "-z",
    ).stdout
    staged = {item for item in staged_raw.split("\0") if item}
    schema_assets = {
        path
        for path in staged
        if path == "dish-task-schema.json" or path.startswith("dish-schema-migrations/")
    }
    if not any(_schema_asset_has_structural_change(runner, worktree, path) for path in schema_assets):
        return

    new = _parse_version(_show(runner, worktree, ":DISH_VERSION"), "staged DISH_VERSION")
    if new is None:
        fail("COMMIT_GUARD_REFUSED", "governed dish protocol/schema changes require staged DISH_VERSION")
    old = _parse_version(_show(runner, worktree, "HEAD:DISH_VERSION"), "HEAD DISH_VERSION")
    if old is None:
        return
    if new["PROTOCOL_VERSION"] == old["PROTOCOL_VERSION"] or new["SCHEMA_VERSION"] == old["SCHEMA_VERSION"]:
        fail(
            "COMMIT_GUARD_REFUSED",
            "staged dish schema/migration changes require both PROTOCOL_VERSION and SCHEMA_VERSION bumps in DISH_VERSION",
        )


def command_commit(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    _claimed_state(task_gid)
    with TaskLock(task_gid):
        state = _claimed_state(task_gid)
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
        if identity.path == repo.primary_top or identity.git_dir == identity.common_dir:
            fail("PROTECTED_PRIMARY", "task-scoped commit refuses the shared primary checkout")
        if identity.branch == "main" or not identity.branch.startswith("agent/"):
            fail("BRANCH_MISMATCH", f"task-scoped commit requires the stored agent/* branch, found {identity.branch!r}")

        explicit = _normalize_explicit_paths(identity.path, list(args.paths))
        scopes = _path_scopes(identity.path, explicit)
        _verify_staged_subset(_staged_paths(runner, identity.path), scopes)

        runner.run(identity.path, "--literal-pathspecs", "add", "--", *explicit)
        staged = _staged_paths(runner, identity.path)
        if not staged:
            fail("NOTHING_TO_COMMIT", "explicit paths contain no staged changes")
        _verify_staged_subset(staged, scopes)

        merge_target_head = getattr(args, "merge_target_head", None)
        merge_target: str | None = None

        # Re-resolve identity after staging and immediately before constructing
        # the commit. A concurrent raw branch move after this point is fenced by
        # update-ref's exact old-head compare-and-swap below.
        before_commit = verify_owned_worktree(runner, repo, state)
        if before_commit.head != identity.head or before_commit.branch != identity.branch:
            fail("BRANCH_MOVED", "owned branch moved while preparing the bounded commit")

        if merge_target_head is not None:
            merge_target = _resolve_merge_target(runner, repo, state, merge_target_head)

        _check_dish_version_guard(runner, identity.path)
        tree = runner.run(identity.path, "write-tree").stdout.strip()
        commit_tree_args = [tree, "-p", identity.head]
        if merge_target is not None:
            commit_tree_args.extend(["-p", merge_target])
        commit = runner.run(
            identity.path,
            "commit-tree",
            *commit_tree_args,
            stdin=args.message.rstrip("\n") + "\n",
        ).stdout.strip()
        if merge_target is not None:
            # The first check establishes the requested target and materializes its
            # object. Re-read the remote after all commit preparation, including
            # commit-tree, so a base move during that window leaves this commit
            # unattached and the owned branch unchanged.
            _require_current_merge_target(runner, repo, state, merge_target)
        ref = f"refs/heads/{identity.branch}"
        moved = runner.run(identity.path, "update-ref", ref, commit, identity.head, check=False)
        if moved.returncode != 0:
            fail(
                "BRANCH_MOVED",
                "owned branch moved during commit; exact-head ref update was refused and the new commit was not attached",
            )

        after = verify_owned_worktree(runner, repo, state)
        if after.head != commit or after.branch != identity.branch:
            fail("COMMIT_VERIFY_FAILED", "post-commit worktree identity does not match the exact created commit")
        parent = runner.sha(identity.path, f"{commit}^1")
        if parent != identity.head:
            fail("COMMIT_VERIFY_FAILED", f"new commit parent {parent} != pre-commit head {identity.head}")
        if merge_target is not None:
            second_parent = runner.sha(identity.path, f"{commit}^2")
            if second_parent != merge_target:
                fail(
                    "MERGE_COMMIT_VERIFY_FAILED",
                    f"new merge commit second parent {second_parent} != verified reconciliation target {merge_target}",
                )

        state["local_head"] = after.head
        state["last_verified_at"] = now_utc()
        atomic_write_json(state_path(task_gid), state)
        payload = payload_from_state("commit", state, after)
        payload["committed_paths"] = sorted(staged)
        payload["previous_head"] = identity.head
        payload["new_head"] = after.head
        payload["merge_target_head"] = merge_target
        payload["next_action"] = f"tools/agent-worktree publish --task {task_gid}"
        return payload
