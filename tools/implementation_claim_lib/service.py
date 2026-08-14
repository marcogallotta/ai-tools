from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from .errors import ClaimError
from .github import GitHubReaderProtocol
from .orchestration import AsanaMirrorProtocol
from .store import ClaimStore


class ClaimCoordinator:
    def __init__(self, store: ClaimStore, *, repository: str, asana: AsanaMirrorProtocol,
                 github: GitHubReaderProtocol | None = None):
        self.store = store
        self.repository = repository
        self.asana = asana
        self.github = github
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def _task_lock(self, task_gid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[task_gid]

    def _repo(self, supplied: str | None) -> str:
        if supplied is not None and supplied != self.repository:
            raise ClaimError("REPOSITORY_DENIED", f"service is scoped to {self.repository}", 403)
        return self.repository

    def _sync(self, claim: dict[str, Any]) -> dict[str, Any]:
        try:
            self.asana.sync(claim)
        except ClaimError as exc:
            exc.current = claim
            raise
        except Exception as exc:
            raise ClaimError("ASANA_UNAVAILABLE", f"Asana synchronization failed: {exc}", 503, current=claim) from exc
        return self.store.mark_asana_synced(self.repository, claim["task_gid"], claim["claim_id"])

    def status(self, task_gid: str) -> dict[str, Any] | None:
        return self.store.read(self.repository, task_gid)

    def dispatch_guard(self, task_gid: str) -> dict[str, Any]:
        claim = self.status(task_gid)
        if claim is None:
            return {"task_gid": str(task_gid), "dispatchable": True, "reason": "no durable claim lineage", "claim": None}
        blocked = bool(claim["dispatch_blocked"])
        return {
            "task_gid": str(task_gid),
            "dispatchable": not blocked,
            "reason": "durable claim lineage exists; continue or replace it by exact generation",
            "claim": claim,
        }

    def sync(self, task_gid: str, claim_id: str) -> dict[str, Any]:
        with self._task_lock(task_gid):
            current = self.store.read(self.repository, task_gid)
            if current is None:
                raise ClaimError("CLAIM_MISSING", "no durable claim exists", 404)
            if current["claim_id"] != claim_id:
                raise ClaimError("OWNERSHIP_CONFLICT", "claim_id is stale", 409, current=current)
            if current["asana_sync_state"] == "synced":
                return current
            return self._sync(current)

    def acquire(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo = self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim = self.store.acquire(
                repository=repo, task_gid=task, owner=str(payload["owner"]), session_id=str(payload["session_id"]),
                host=str(payload["host"]), authoring_base_sha=str(payload["authoring_base_sha"]), branch=payload.get("branch"),
            )
            return self._sync(claim)

    def takeover(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo = self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim = self.store.takeover(
                repository=repo, task_gid=task, expected_claim_id=str(payload["expected_claim_id"]),
                owner=str(payload["owner"]), session_id=str(payload["session_id"]), host=str(payload["host"]),
                authoring_base_sha=str(payload["authoring_base_sha"]), reason=str(payload["reason"]),
                liveness_evidence=str(payload["liveness_evidence"]),
            )
            return self._sync(claim)

    def authorize(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        return self.store.authorize(self.repository, str(payload["task_gid"]), str(payload["claim_id"]), branch=payload.get("branch"))

    def renew(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        return self.store.renew(self.repository, str(payload["task_gid"]), str(payload["claim_id"]))

    def bind_branch(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim = self.store.bind_branch(self.repository, task, str(payload["claim_id"]), str(payload["branch"]))
            return self._sync(claim)

    def bind_pr(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim = self.store.bind_pr(
                self.repository, task, str(payload["claim_id"]), pr_number=int(payload["pr_number"]), pr_head=str(payload["pr_head"])
            )
            return self._sync(claim)

    def begin_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim, publication, replay = self.store.begin_publication(
                self.repository, task, str(payload["claim_id"]), branch=str(payload["branch"]),
                expected_head=payload.get("expected_head"), proposed_head=str(payload["proposed_head"]), request_id=str(payload["request_id"]),
            )
            if not replay:
                claim = self._sync(claim)
            return {"claim": claim, "publication": publication, "replay": replay}

    def complete_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim, publication = self.store.complete_publication(
                self.repository, task, str(payload["claim_id"]), request_id=str(payload["request_id"]),
                result_head=str(payload["result_head"]), pr_number=(int(payload["pr_number"]) if payload.get("pr_number") is not None else None),
            )
            claim = self._sync(claim)
            return {"claim": claim, "publication": publication}

    def abort_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim = self.store.abort_publication(self.repository, task, str(payload["claim_id"]), request_id=str(payload["request_id"]))
            return self._sync(claim)

    def reconcile_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        if self.github is None:
            raise ClaimError("GITHUB_UNAVAILABLE", "claim service has no GitHub read adapter configured", 503)
        task = str(payload["task_gid"])
        request_id = str(payload["request_id"])
        claim_id = str(payload["claim_id"])
        with self._task_lock(task):
            publication = self.store.publication(self.repository, task, request_id)
            if publication is None:
                raise ClaimError("PUBLICATION_MISSING", "publication intent does not exist", 404)
            current = self.store.read(self.repository, task)
            if current is None or current["claim_id"] != claim_id or publication["claim_id"] != claim_id:
                raise ClaimError("OWNERSHIP_CONFLICT", "publication belongs to a stale claim", 409, current=current)
            if publication["state"] == "completed":
                if current["asana_sync_state"] != "synced":
                    current = self._sync(current)
                return {"claim": current, "publication": publication, "reconciled": True}
            live_head = self.github.branch_head(self.repository, publication["branch"])
            if live_head == publication["proposed_head"]:
                claim, publication = self.store.complete_publication(
                    self.repository, task, claim_id, request_id=request_id, result_head=publication["proposed_head"],
                    pr_number=publication.get("pr_number"),
                )
                claim = self._sync(claim)
                return {"claim": claim, "publication": publication, "reconciled": True}
            if live_head == publication["expected_head"]:
                if current["asana_sync_state"] != "synced":
                    current = self._sync(current)
                return {"claim": current, "publication": publication, "reconciled": False, "branch_unchanged": True}
            raise ClaimError(
                "HEAD_MOVED",
                f"live branch head {live_head!r} matches neither expected nor proposed publication head",
                409,
                current=current,
            )

    def review_ready(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim = self.store.review_ready(
                self.repository, task, str(payload["claim_id"]), pr_number=int(payload["pr_number"]), pr_head=str(payload["pr_head"])
            )
            return self._sync(claim)

    def release(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim = self.store.release(self.repository, task, str(payload["claim_id"]), reason=str(payload["reason"]))
            return self._sync(claim)

    def supersede(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._repo(payload.get("repository"))
        task = str(payload["task_gid"])
        with self._task_lock(task):
            claim = self.store.supersede(self.repository, task, str(payload["claim_id"]), reason=str(payload["reason"]))
            return self._sync(claim)
