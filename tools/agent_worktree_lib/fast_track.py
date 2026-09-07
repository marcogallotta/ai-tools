from __future__ import annotations

import argparse

from .common import GitRunner, fail, now_utc, require_task_gid
from .commit import _claimed_state as _commit_claimed_state, command_commit
from .fast_track_auth import FastTrackAuthorization, _fallback, _live_authorization, _parse_authorization_story
from .fast_track_guard import (
    _commit_changed_paths,
    _require_bounded,
    _require_fresh_base,
    _validated_context,
    _worktree_changed_paths,
    assert_fast_track_worktree,
)
from .operations import payload_from_state, remote_ref_sha, resolve_repository_from_state, verify_owned_worktree
from .publish_cleanup import command_publish
from .state import TaskLock, atomic_write_json, state_path


def build_fast_track_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)
    commit = sub.add_parser("fast-track-commit")
    commit.add_argument("--task", required=True)
    commit.add_argument("--authorization-story", required=True)
    commit.add_argument("-m", "--message", required=True)
    commit.add_argument("--json", action="store_true")
    publish = sub.add_parser("fast-track-publish")
    publish.add_argument("--task", required=True)
    publish.add_argument("--authorization-story", required=True)
    publish.add_argument("--json", action="store_true")
    return parser

def command_fast_track_commit(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    state, _repo, identity, auth = _validated_context(args, runner)
    dirty = _worktree_changed_paths(runner, identity.path)
    if not dirty:
        fail("NOTHING_TO_COMMIT", "fast-track worktree contains no changed paths")
    _require_bounded(dirty, auth, label="worktree change")

    delegated = argparse.Namespace(
        task=state["task_gid"],
        message=args.message,
        merge_target_head=None,
        paths=sorted(dirty),
    )
    payload = command_commit(delegated, runner)
    payload.update(
        {
            "fast_track_mode": auth.mode,
            "authorization_story": auth.story_gid,
            "authorized_paths": list(auth.paths),
            "skip_review": auth.skip_review,
            "validation": auth.validation,
        }
    )
    payload["next_action"] = f"tools/agent-worktree fast-track-publish --task {auth.task_gid} --authorization-story {auth.story_gid}"
    return payload


def command_fast_track_publish(args: argparse.Namespace, runner: GitRunner) -> dict[str, Any]:
    task_gid = require_task_gid(args.task)
    state, repo, identity, auth = _validated_context(args, runner)
    dirty = _worktree_changed_paths(runner, identity.path)
    if dirty:
        _fallback("worktree is dirty after the authorized commit")
    changed = _commit_changed_paths(runner, identity.path, auth.base_head, identity.head)
    if not changed:
        fail("FAST_TRACK_NOTHING_TO_PUBLISH", "authorized branch contains no change from the recorded base")
    _require_bounded(changed, auth, label="published change")

    ancestor = runner.run(identity.path, "merge-base", "--is-ancestor", auth.base_head, identity.head, check=False)
    if ancestor.returncode != 0:
        _fallback("authorized branch head is not a fast-forward descendant of the recorded base")

    if auth.mode == "FAST-TRACK":
        payload = command_publish(args, runner)
        payload.update(
            {
                "fast_track_mode": auth.mode,
                "authorization_story": auth.story_gid,
                "authorized_paths": list(auth.paths),
                "skip_review": auth.skip_review,
                "validation": auth.validation,
            }
        )
        proof = (
            "focused executable proof of the changed accepted/user-visible invariant before Integration"
            if auth.validation == "executable-proof"
            else "the cheapest meaningful validation/readback; executable tests are not required when they add no meaningful evidence"
        )
        review = "formal Review may be skipped for this exact grant" if auth.skip_review else "independent exact-head Review remains required"
        payload["next_action"] = f"open/update the PR; record {proof}; {review}"
        return payload

    count = int(runner.run(identity.path, "rev-list", "--count", f"{auth.base_head}..{identity.head}").stdout.strip())
    if count != 1:
        _fallback(f"TRIVIAL publication requires exactly one authorized commit, found {count}")

    with TaskLock(task_gid):
        # Re-read every mutable identity immediately before the externally visible write.
        state = _commit_claimed_state(task_gid, runner)
        repo = resolve_repository_from_state(runner, state)
        identity = verify_owned_worktree(runner, repo, state)
        assert_fast_track_worktree(identity, repo)
        auth = _live_authorization(task_gid, getattr(args, "authorization_story", None), state)
        _require_fresh_base(runner, repo, auth)
        dirty = _worktree_changed_paths(runner, identity.path)
        if dirty:
            _fallback("worktree became dirty before TRIVIAL publication")
        changed = _commit_changed_paths(runner, identity.path, auth.base_head, identity.head)
        _require_bounded(changed, auth, label="published change")
        ancestor = runner.run(identity.path, "merge-base", "--is-ancestor", auth.base_head, identity.head, check=False)
        if ancestor.returncode != 0:
            _fallback("TRIVIAL head is no longer a fast-forward descendant of the recorded base")

        refspec = f"{identity.head}:{auth.base_ref}"
        pushed = runner.run(repo.source_top, "push", repo.origin_url, refspec, check=False)
        if pushed.returncode != 0:
            observed = remote_ref_sha(runner, repo, auth.base_ref)
            if observed != auth.base_head:
                _fallback(f"{auth.base_ref} moved concurrently to {observed}")
            fail("FAST_TRACK_PUBLISH_FAILED", f"TRIVIAL fast-forward publication failed: {pushed.stderr.strip()}")
        verified = remote_ref_sha(runner, repo, auth.base_ref)
        if verified != identity.head:
            fail("FAST_TRACK_PUBLISH_VERIFY_FAILED", f"authoritative {auth.base_ref} is {verified}, not intended {identity.head}")

        state["fast_track_publication"] = {
            "mode": auth.mode,
            "authorization_story": auth.story_gid,
            "base_head": auth.base_head,
            "published_head": identity.head,
            "paths": sorted(changed),
            "verified_at": now_utc(),
        }
        state["target_current_head"] = identity.head
        state["last_verified_at"] = now_utc()
        atomic_write_json(state_path(task_gid), state)
        payload = payload_from_state("fast-track-publish", state, identity, target_head=identity.head)
        payload.update(
            {
                "fast_track_mode": auth.mode,
                "authorization_story": auth.story_gid,
                "authorized_paths": list(auth.paths),
                "published_target": auth.base_ref,
                "published_head": identity.head,
                "skip_review": True,
                "next_action": "none; exact authorized TRIVIAL source publication is complete",
            }
        )
        return payload
