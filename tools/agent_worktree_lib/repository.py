from __future__ import annotations

from pathlib import Path

from .common import (
    AgentWorktreeError, GitRunner, Repository, current_symbolic_branch, fail,
    find_worktree_record, normalize_origin, reject_url_rewrites, worktree_records,
)

def discover_repository(runner: GitRunner, repo_path: Path) -> Repository:
    repo_path = repo_path.expanduser().resolve()
    bare = runner.run(repo_path, "rev-parse", "--is-bare-repository").stdout.strip()
    if bare != "false":
        fail("REPOSITORY_IDENTITY", "agent-worktree must be invoked from a non-bare checkout/worktree")
    source_top = runner.path(repo_path, "--show-toplevel")
    source_git_dir = runner.path(source_top, "--git-dir")
    common_dir = runner.path(source_top, "--git-common-dir")
    records = worktree_records(runner, source_top)
    source_record = find_worktree_record(records, source_top)
    if source_record is None:
        fail("WORKTREE_REGISTRY", f"invoking worktree is not registered: {source_top}")
    source_head = runner.sha(source_top, "HEAD")
    if source_record.get("HEAD") != source_head:
        fail("WORKTREE_REGISTRY", "invoking worktree HEAD does not match Git worktree registry")
    source_branch = current_symbolic_branch(runner, source_top)
    if source_branch is None:
        if "branch" in source_record:
            fail("WORKTREE_REGISTRY", "detached invoking worktree disagrees with Git worktree registry")
    elif source_record.get("branch") != f"refs/heads/{source_branch}":
        fail("WORKTREE_REGISTRY", "invoking worktree branch does not match Git worktree registry")

    primary_candidates: list[Path] = []
    for record in records:
        raw = record.get("worktree")
        if not raw:
            continue
        candidate = Path(raw).resolve()
        try:
            candidate_git_dir = runner.path(candidate, "--git-dir")
        except AgentWorktreeError:
            continue
        if candidate_git_dir == common_dir:
            primary_candidates.append(candidate)
    if len(primary_candidates) != 1:
        fail("REPOSITORY_IDENTITY", "could not resolve exactly one primary worktree from the shared common-dir")
    primary_top = primary_candidates[0]
    if source_git_dir == common_dir and source_top != primary_top:
        fail("REPOSITORY_IDENTITY", "primary worktree identity is internally inconsistent")

    reject_url_rewrites(runner, source_top)
    urls = [line.strip() for line in runner.run(source_top, "remote", "get-url", "--all", "origin").stdout.splitlines() if line.strip()]
    if len(urls) != 1:
        fail("ORIGIN_IDENTITY", f"origin must have exactly one fetch URL; found {len(urls)}")
    origin_url = urls[0]
    origin_id = normalize_origin(origin_url)
    return Repository(source_top, primary_top, common_dir, origin_url, origin_id)
