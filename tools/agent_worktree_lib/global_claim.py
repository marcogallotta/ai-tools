from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from implementation_claim_lib.client import ClaimServiceClient
from implementation_claim_lib.errors import ClaimError
from implementation_claim_lib.github import GitHubReaderProtocol
from implementation_claim_lib.orchestration import NullAsanaMirror
from implementation_claim_lib.service import ClaimCoordinator
from implementation_claim_lib.store import ClaimStore

from .common import fail

GLOBAL_CLAIM_ENV = "DISH_IMPLEMENTATION_GLOBAL_CLAIM_ID"
GLOBAL_WRITER_ENV = "DISH_IMPLEMENTATION_GLOBAL_WRITER_CAPABILITY"
TEST_DB_ENV = "DISH_IMPLEMENTATION_CLAIM_TEST_DB"
TEST_MODE_ENV = "DISH_IMPLEMENTATION_CLAIM_TESTING"


class _DirectTestClient:
    """Subprocess-safe direct adapter used only by repository tests."""

    def __init__(self, path: str):
        self._coordinator = ClaimCoordinator(
            ClaimStore(Path(path)), repository="marcogallotta/ai-tools", asana=NullAsanaMirror()
        )
        self.repository = "marcogallotta/ai-tools"

    def status(self, task_gid: str) -> dict[str, Any] | None:
        return self._coordinator.status(task_gid)

    def dispatch_guard(self, task_gid: str) -> dict[str, Any]:
        return self._coordinator.dispatch_guard(task_gid)

    def acquire(self, **kwargs: Any) -> dict[str, Any]:
        return self._coordinator.acquire({"repository": self.repository, **kwargs})

    def takeover(self, **kwargs: Any) -> dict[str, Any]:
        return self._coordinator.takeover({"repository": self.repository, **kwargs}, recovery_authorized=True)

    def sync(self, *, task_gid: str, claim_id: str, writer_capability: str) -> dict[str, Any]:
        return self._coordinator.sync({"repository": self.repository, "task_gid": task_gid, "claim_id": claim_id, "writer_capability": writer_capability})

    def authorize(self, *, task_gid: str, claim_id: str, writer_capability: str, branch: str | None = None) -> dict[str, Any]:
        return self._coordinator.authorize({"repository": self.repository, "task_gid": task_gid, "claim_id": claim_id, "writer_capability": writer_capability, "branch": branch})

    def bind_pr(self, *, task_gid: str, claim_id: str, writer_capability: str, pr_number: int, pr_head: str) -> dict[str, Any]:
        return self._coordinator.bind_pr({"repository": self.repository, "task_gid": task_gid, "claim_id": claim_id, "writer_capability": writer_capability, "pr_number": pr_number, "pr_head": pr_head})

    def begin_publication(self, **kwargs: Any) -> dict[str, Any]:
        return self._coordinator.begin_publication({"repository": self.repository, **kwargs})

    def complete_publication(self, **kwargs: Any) -> dict[str, Any]:
        return self._coordinator.complete_publication({"repository": self.repository, **kwargs})

    def abort_publication(self, **kwargs: Any) -> dict[str, Any]:
        return self._coordinator.abort_publication({"repository": self.repository, **kwargs})


Client = ClaimServiceClient | _DirectTestClient


def client() -> Client:
    test_db = os.environ.get(TEST_DB_ENV)
    if test_db:
        if os.environ.get(TEST_MODE_ENV) != "1":
            fail("GLOBAL_CLAIM_TEST_MODE_DENIED", f"{TEST_DB_ENV} is test-only and requires {TEST_MODE_ENV}=1")
        return _DirectTestClient(test_db)
    try:
        return ClaimServiceClient.from_env()
    except ClaimError as exc:
        fail(exc.code, exc.message)


def _call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except ClaimError as exc:
        fail(exc.code, exc.message)


def status(task_gid: str) -> dict[str, Any] | None:
    c = client()
    return _call(c.status, task_gid)


def _writer_capability(explicit: str | None = None) -> str:
    capability = explicit or os.environ.get(GLOBAL_WRITER_ENV)
    if not capability:
        fail("WRITER_AUTHORITY_REQUIRED", f"private global writer capability is required via {GLOBAL_WRITER_ENV}")
    return capability


def authorize(task_gid: str, claim_id: str, branch: str, *, writer_capability: str | None = None) -> dict[str, Any]:
    c = client()
    return _call(c.authorize, task_gid=task_gid, claim_id=claim_id, writer_capability=_writer_capability(writer_capability), branch=branch)


def acquire(*, task_gid: str, owner: str, session_id: str, host: str, base_sha: str, branch: str) -> dict[str, Any]:
    c = client()
    return _call(
        c.acquire,
        task_gid=task_gid, owner=owner, session_id=session_id, host=host,
        authoring_base_sha=base_sha, branch=branch,
    )


def takeover(*, task_gid: str, expected_claim_id: str, owner: str, session_id: str, host: str,
             base_sha: str, reason: str, liveness_evidence: str) -> dict[str, Any]:
    c = client()
    return _call(
        c.takeover,
        task_gid=task_gid, expected_claim_id=expected_claim_id, owner=owner, session_id=session_id,
        host=host, authoring_base_sha=base_sha, reason=reason, liveness_evidence=liveness_evidence,
    )


def sync(task_gid: str, claim_id: str, *, writer_capability: str | None = None) -> dict[str, Any]:
    c = client()
    return _call(c.sync, task_gid=task_gid, claim_id=claim_id, writer_capability=_writer_capability(writer_capability))


def bind_pr(task_gid: str, claim_id: str, pr_number: int, pr_head: str, *, writer_capability: str | None = None) -> dict[str, Any]:
    c = client()
    return _call(c.bind_pr, task_gid=task_gid, claim_id=claim_id, writer_capability=_writer_capability(writer_capability), pr_number=pr_number, pr_head=pr_head)


def begin_publication(*, task_gid: str, claim_id: str, branch: str, expected_head: str | None,
                      proposed_head: str, request_id: str, writer_capability: str | None = None) -> dict[str, Any]:
    c = client()
    return _call(
        c.begin_publication,
        task_gid=task_gid, claim_id=claim_id, writer_capability=_writer_capability(writer_capability), branch=branch, expected_head=expected_head,
        proposed_head=proposed_head, request_id=request_id,
    )


def complete_publication(*, task_gid: str, claim_id: str, request_id: str, result_head: str,
                         writer_capability: str | None = None) -> dict[str, Any]:
    c = client()
    return _call(c.complete_publication, task_gid=task_gid, claim_id=claim_id, writer_capability=_writer_capability(writer_capability), request_id=request_id, result_head=result_head)


def abort_publication(*, task_gid: str, claim_id: str, request_id: str,
                      writer_capability: str | None = None) -> dict[str, Any]:
    c = client()
    return _call(c.abort_publication, task_gid=task_gid, claim_id=claim_id, writer_capability=_writer_capability(writer_capability), request_id=request_id)
