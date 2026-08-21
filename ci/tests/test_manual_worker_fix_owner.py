from __future__ import annotations

import pytest
import test_pr_lifecycle as base

p = base.pr_lifecycle
OWNER = "1217657236042386"
OTHER = "1217443403986570"


def _github(body: str, *, head: str = base.HEAD, verdict: str = "BLOCK"):
    gh = base.FakeGitHub(base.pr(head=head, body=body))
    gh.reviews = [base.review(head=base.HEAD, verdict=verdict, review_id=44)]
    return gh


def _bind(gh, *, task: str = OWNER, blocked_head: str = base.HEAD):
    return p.bind_manual_worker_block_fix(
        gh,
        31,
        task=task,
        blocked_head=blocked_head,
        block_review_id="44",
    )


def test_matching_explicit_owner_binds_and_returns_verified_task():
    gh = _github(f"<!-- dish-owning-task:v1 task={OWNER} -->\nOwning task: {OWNER}")
    fix = _bind(gh)
    assert (fix.task, fix.pr, fix.branch, fix.blocked_head, fix.block_review_id) == (
        OWNER,
        31,
        "agent/test",
        base.HEAD,
        "44",
    )


def test_wrong_supplied_task_fails_closed():
    gh = _github(f"Owning task: {OWNER}")
    with pytest.raises(p.LifecycleError, match="does not match authoritative PR owning task"):
        _bind(gh, task=OTHER)


@pytest.mark.parametrize(
    "body",
    [
        "no owning task declaration",
        f"Owning task: {OWNER} {OTHER}",
        f"<!-- dish-owning-task:v1 task={OWNER} -->\nOwning task: {OTHER}",
    ],
)
def test_missing_ambiguous_or_conflicting_owner_fails_closed(body):
    gh = _github(body)
    with pytest.raises(p.LifecycleError, match="explicit owning task"):
        _bind(gh)


def test_moved_head_still_fails_closed():
    gh = _github(f"Owning task: {OWNER}", head=base.NEW_HEAD)
    with pytest.raises(p.LifecycleError, match="candidate moved"):
        _bind(gh)


def test_invalid_formal_block_still_fails_closed():
    gh = _github(f"Owning task: {OWNER}", verdict="MERGE")
    with pytest.raises(p.LifecycleError, match="one exact formal BLOCK review"):
        _bind(gh)
