#!/usr/bin/env python3
"""Durable PR lifecycle status and dispatch for Dish.

The dispatcher is deliberately stateless: GitHub PR metadata, reviews, comments,
checks, and linked Asana identity are the recoverable control surface. Local
process memory is only a cache for one poll iteration.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import uuid

import pr_gate


LEASE_VERSION = "v1"
LEASE_STALE_AFTER = timedelta(minutes=60)
LEASE_MARKER = "dish-agent-lease:v1"
LEASE_RELEASE_MARKER = "dish-agent-lease-release:v1"
LOCAL_HANDOFF_MARKER = "dish-local-handoff:v1"
LOCAL_COMPLETION_MARKER = "dish-local-completion:v1"
HUMAN_NOTICE_MARKER = "dish-human-notice:v1"
REVIEW_ROUTE_MARKER = "dish-review-route:v1"
IMPLEMENTATION_CONTINUATION_MARKER = "dish-implementation-continuation:v1"
EXTERNAL_DEPENDENCY_MARKER = "dish-external-dependency:v1"
DISPATCH_OWNER = "pr-lifecycle"
WORKSPACE_API_ROOT = "https://api.chatgpt.com/v1"
WORKSPACE_RUNS_BETA = "workspace_agent_runs=v1"
TASK_GID_RE = re.compile(r"(?<!\d)(\d{16})(?!\d)")
TESTS_TO_RUN_RE = re.compile(r"(?im)^TESTS TO RUN:\s*(?P<value>.+?)\s*$")
AUTHORING_EVIDENCE_PENDING_RE = re.compile(
    r"(?im)^IMPLEMENTATION EVIDENCE PENDING:\s*(?P<value>[^\n]+?)\s*$"
)
LOCAL_IMPLEMENTATION_RE = re.compile(
    r"(?im)^LOCAL IMPLEMENTATION COMPLETION REQUIRED:\s*(?P<value>.+?)\s*$"
)
INTEGRATION_BLOCK_RE = re.compile(r"(?im)^INTEGRATION BLOCKED BY:\s*(?P<value>.+?)\s*$")
REVIEW_CLASS_RE = re.compile(r"(?im)^REVIEW CLASS:\s*(?P<value>[^\n]+?)\s*$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

AFTER_FIX_RE = re.compile(
    r"(?im)^(?:AFTER-FIX DISPOSITION:\s*)?(FOCUSED RECHECK|MECHANICAL CHECK ONLY|DOMAIN DEEP RECHECK|NEW SPECIALIST REVIEW|NORMAL MERGE REVIEW)\s*$"
)


class LifecycleError(RuntimeError):
    """A durable lifecycle action could not be completed safely."""


class HTTPError(LifecycleError):
    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}{': ' + body if body else ''}")
        self.status = status
        self.body = body


class LifecycleState(str, Enum):
    AUTHORING = "authoring_implementation_in_progress"
    IMPLEMENTATION_CONTINUATION_REQUIRED = "implementation_continuation_required"
    REVIEW_READY = "review_ready"
    REVIEW_IN_PROGRESS = "review_in_progress"
    CHANGES_REQUESTED = "changes_requested_fix_in_progress"
    REVIEW_PASSED = "review_passed_evaluating_gates"
    LOCAL_IMPLEMENTATION_REQUIRED = "local_implementation_completion_required"
    LOCAL_CERTIFICATION_REQUIRED = "local_certification_required"
    WAITING_CI = "waiting_ci_certification"
    WAITING_EXTERNAL_DEPENDENCY = "waiting_external_dependency"
    INTEGRATION_READY = "integration_ready"
    MERGING = "merging_integration_in_progress"
    MERGED = "merged"
    CLOSED = "closed_superseded"


STATE_LABELS: dict[LifecycleState, str] = {
    LifecycleState.AUTHORING: "AUTHORING / IMPLEMENTATION IN PROGRESS",
    LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED: "IMPLEMENTATION CONTINUATION REQUIRED",
    LifecycleState.REVIEW_READY: "REVIEW READY",
    LifecycleState.REVIEW_IN_PROGRESS: "REVIEW IN PROGRESS",
    LifecycleState.CHANGES_REQUESTED: "CHANGES REQUESTED / FIX IN PROGRESS",
    LifecycleState.REVIEW_PASSED: "REVIEW PASSED / EVALUATING GATES",
    LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED: "LOCAL IMPLEMENTATION COMPLETION REQUIRED",
    LifecycleState.LOCAL_CERTIFICATION_REQUIRED: "LOCAL CERTIFICATION REQUIRED",
    LifecycleState.WAITING_CI: "WAITING CI / CERTIFICATION",
    LifecycleState.WAITING_EXTERNAL_DEPENDENCY: "WAITING ON EXTERNAL DEPENDENCY",
    LifecycleState.INTEGRATION_READY: "INTEGRATION READY",
    LifecycleState.MERGING: "MERGING / INTEGRATION IN PROGRESS",
    LifecycleState.MERGED: "MERGED",
    LifecycleState.CLOSED: "CLOSED / SUPERSEDED",
}


@dataclass(frozen=True)
class ExternalDependency:
    action: str
    task_gid: str
    owner_pr: int | None
    check: str
    main_sha: str
    evidence: str
    reason: str | None
    timestamp: datetime
    comment_id: int
    marker_index: int

    def json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Lease:
    phase: str
    head: str
    lease: str
    timestamp: datetime
    owner: str | None = None
    review_class: str | None = None
    comment_id: int | None = None


@dataclass(frozen=True)
class LocalWork:
    kind: str
    required: bool
    instruction: str | None = None
    completed: bool = False
    handoff_present: bool = False


@dataclass
class PRLifecycle:
    number: int
    url: str
    title: str
    head: str
    branch: str
    base: str
    draft: bool
    state: LifecycleState
    state_label: str
    task_ids: list[str] = field(default_factory=list)
    authoring_evidence: str | None = None
    review_class: str | None = None
    review_verdict: str | None = None
    reviewed_head: str | None = None
    active_leases: list[dict[str, Any]] = field(default_factory=list)
    local_work: list[dict[str, Any]] = field(default_factory=list)
    gate: dict[str, Any] | None = None
    external_dependency: dict[str, Any] | None = None
    residual_reason: str | None = None
    human_action: str | None = None
    asana: list[dict[str, Any]] = field(default_factory=list)

    def json(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value


class GitHubBackend(Protocol):
    repository: str

    def list_prs(self, *, include_closed: bool = False) -> list[dict[str, Any]]: ...
    def get_pr(self, number: int) -> dict[str, Any]: ...
    def get_comments(self, number: int) -> list[dict[str, Any]]: ...
    def get_reviews(self, number: int) -> list[dict[str, Any]]: ...
    def get_combined_status(self, sha: str) -> dict[str, Any]: ...
    def get_workflow_runs(self) -> dict[str, Any]: ...
    def add_comment(self, number: int, body: str) -> dict[str, Any]: ...
    def close_pr(self, number: int) -> dict[str, Any]: ...
    def get_branch(self, branch: str) -> dict[str, Any] | None: ...
    def merge(self, number: int, *, expected_head: str, method: str) -> dict[str, Any]: ...


class AsanaBackend(Protocol):
    def get_task(self, gid: str) -> dict[str, Any]: ...


class JSONHTTPClient:
    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        payload = None
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        req = urlrequest.Request(url, data=payload, headers=request_headers, method=method)
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                parsed: Any = json.loads(raw) if raw else {}
                return response.status, dict(response.headers.items()), parsed
        except urlerror.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            message = exc.reason or "request failed"
            try:
                parsed_error = json.loads(raw)
                message = parsed_error.get("message") or parsed_error.get("error") or message
            except json.JSONDecodeError:
                pass
            raise HTTPError(exc.code, str(message), raw[:500]) from exc
        except urlerror.URLError as exc:
            raise LifecycleError(f"request failed for {url}: {exc.reason}") from exc


class GitHubREST:
    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_root: str = "https://api.github.com",
        http: JSONHTTPClient | None = None,
    ) -> None:
        if "/" not in repository:
            raise LifecycleError("repository must be owner/name")
        self.repository = repository
        self.api_root = api_root.rstrip("/")
        self.http = http or JSONHTTPClient()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dish-pr-lifecycle/1",
        }

    def _url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        url = f"{self.api_root}/repos/{self.repository}/{path.lstrip('/')}"
        if query:
            url += "?" + urlparse.urlencode({k: v for k, v in query.items() if v is not None})
        return url

    def _get_paginated(self, path: str, *, query: Mapping[str, Any] | None = None) -> list[Any]:
        page = 1
        values: list[Any] = []
        while True:
            params = dict(query or {})
            params.update({"per_page": 100, "page": page})
            _, _, payload = self.http.request("GET", self._url(path, params), headers=self.headers)
            if not isinstance(payload, list):
                raise LifecycleError(f"expected list from GitHub {path}")
            values.extend(payload)
            if len(payload) < 100:
                return values
            page += 1

    def list_prs(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        state = "all" if include_closed else "open"
        return [dict(item) for item in self._get_paginated("pulls", query={"state": state, "sort": "updated", "direction": "desc"})]

    def get_pr(self, number: int) -> dict[str, Any]:
        _, _, value = self.http.request("GET", self._url(f"pulls/{number}"), headers=self.headers)
        if not isinstance(value, dict):
            raise LifecycleError("GitHub pull request response was not an object")
        return value

    def get_comments(self, number: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self._get_paginated(f"issues/{number}/comments")]

    def get_reviews(self, number: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self._get_paginated(f"pulls/{number}/reviews")]

    def get_combined_status(self, sha: str) -> dict[str, Any]:
        _, _, value = self.http.request("GET", self._url(f"commits/{sha}/status"), headers=self.headers)
        if not isinstance(value, dict):
            raise LifecycleError("GitHub combined status response was not an object")
        return value

    def get_workflow_runs(self) -> dict[str, Any]:
        _, _, value = self.http.request(
            "GET",
            self._url("actions/runs", {"event": "pull_request_review", "per_page": 100}),
            headers=self.headers,
        )
        if not isinstance(value, dict):
            raise LifecycleError("GitHub workflow-runs response was not an object")
        return value

    def add_comment(self, number: int, body: str) -> dict[str, Any]:
        _, _, value = self.http.request(
            "POST", self._url(f"issues/{number}/comments"), headers=self.headers, body={"body": body}
        )
        if not isinstance(value, dict):
            raise LifecycleError("GitHub comment response was not an object")
        return value

    def close_pr(self, number: int) -> dict[str, Any]:
        _, _, value = self.http.request(
            "PATCH", self._url(f"pulls/{number}"), headers=self.headers, body={"state": "closed"}
        )
        if not isinstance(value, dict):
            raise LifecycleError("GitHub close pull request response was not an object")
        return value

    def get_branch(self, branch: str) -> dict[str, Any] | None:
        encoded = urlparse.quote(branch, safe="")
        try:
            _, _, value = self.http.request("GET", self._url(f"branches/{encoded}"), headers=self.headers)
        except HTTPError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(value, dict):
            raise LifecycleError("GitHub branch response was not an object")
        return value

    def merge(self, number: int, *, expected_head: str, method: str) -> dict[str, Any]:
        _, _, value = self.http.request(
            "PUT",
            self._url(f"pulls/{number}/merge"),
            headers=self.headers,
            body={"sha": expected_head, "merge_method": method},
        )
        if not isinstance(value, dict):
            raise LifecycleError("GitHub merge response was not an object")
        return value


class AsanaREST:
    def __init__(self, token: str, *, api_root: str = "https://app.asana.com/api/1.0", http: JSONHTTPClient | None = None) -> None:
        self.api_root = api_root.rstrip("/")
        self.http = http or JSONHTTPClient()
        self.headers = {"Authorization": f"Bearer {token}", "User-Agent": "dish-pr-lifecycle/1"}

    def get_task(self, gid: str) -> dict[str, Any]:
        query = urlparse.urlencode({"opt_fields": "gid,name,notes,completed,completed_at,modified_at,permalink_url"})
        _, _, value = self.http.request("GET", f"{self.api_root}/tasks/{gid}?{query}", headers=self.headers)
        if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
            raise LifecycleError(f"Asana task {gid} response was not an object")
        return dict(value["data"])


@dataclass(frozen=True)
class WorkspaceDispatchResult:
    idempotency_key: str
    conversation_url: str | None
    run_id: str | None


class WorkspaceAgentDispatcher:
    def __init__(
        self,
        *,
        access_token: str,
        review_trigger_id: str | None,
        api_root: str = WORKSPACE_API_ROOT,
        http: JSONHTTPClient | None = None,
    ) -> None:
        self.access_token = access_token
        self.review_trigger_id = review_trigger_id
        self.api_root = api_root.rstrip("/")
        self.http = http or JSONHTTPClient()

    @staticmethod
    def idempotency_key(repository: str, number: int, head: str, review_class: str) -> str:
        identity = f"dish-review:v1:{repository}:{number}:{head}:{review_class}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def trigger_id_for(self, review_class: str) -> str | None:
        # domain:<name> (and legacy specialist:<name>) deepen scrutiny inside the same
        # ordinary Review Workspace Agent; they never select a different trigger/reviewer.
        return self.review_trigger_id

    def dispatch(
        self,
        *,
        repository: str,
        pr_number: int,
        pr_url: str,
        head: str,
        review_class: str,
        task_ids: Iterable[str],
    ) -> WorkspaceDispatchResult:
        trigger_id = self.trigger_id_for(review_class)
        if not self.access_token:
            raise LifecycleError("Workspace Agent access token is unavailable")
        if not trigger_id:
            raise LifecycleError("published ChatGPT Review Workspace Agent trigger is unavailable")
        key = self.idempotency_key(repository, pr_number, head, review_class)
        task_identity = ", ".join(task_ids) if task_ids else "none linked"
        domain_hint = ""
        if review_class.startswith("domain:") or review_class.startswith("specialist:"):
            domain_hint = (
                f" This is domain-sensitive work in {review_class.split(':', 1)[1]}: deepen your own "
                "scrutiny of that domain as part of this one formal Review rather than deferring to a "
                "separate specialist reviewer; if a genuine evidence/tool/environment boundary applies "
                "(e.g. native/isolated execution, production-only authority, TEST-only local certification, "
                "or an actual external expert), say so explicitly and name the exact parallel handoff."
            )
        prompt = (
            "Review the exact GitHub pull request as a Dish Review agent. "
            f"Repository: {repository}. PR: {pr_url} (#{pr_number}). "
            f"Exact current head SHA: {head}. Review type: {review_class}.{domain_hint} "
            f"Owning Asana task identity: {task_identity}. "
            "Read and follow the current repository dish/docs/agents/review.md contract. "
            "Re-read the PR head before submitting. The authoritative completion artifact must be a formal "
            "GitHub COMMENT review anchored to this exact head with VERDICT: MERGE or VERDICT: BLOCK; "
            "do not rely on agent-chat output as review state."
        )
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "OpenAI-Beta": WORKSPACE_RUNS_BETA,
            "Idempotency-Key": key,
            "Content-Type": "application/json",
        }
        _, _, value = self.http.request(
            "POST",
            f"{self.api_root}/workspace_agents/{trigger_id}/trigger",
            headers=headers,
            body={
                "conversation_key": f"dish-pr-{repository.replace('/', '-')}-{pr_number}-{head}",
                "input": prompt,
            },
        )
        if not isinstance(value, dict):
            raise LifecycleError("Workspace Agent trigger response was not an object")
        return WorkspaceDispatchResult(
            idempotency_key=key,
            conversation_url=value.get("conversation_url"),
            run_id=value.get("agent_trigger_run_id"),
        )


class LocalReviewDispatcher:
    def __init__(self, command: str | None) -> None:
        self.command = command

    def dispatch(self, context: dict[str, Any]) -> None:
        if not self.command:
            raise LifecycleError("bounded local reviewer command is unavailable")
        completed = subprocess.run(
            shlex.split(self.command),
            input=json.dumps(context),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise LifecycleError(
                f"bounded local reviewer failed with exit {completed.returncode}{': ' + detail if detail else ''}"
            )


class ImplementationFixDispatcher:
    """Invoke the configured existing implementation/fix consumer with exact-head BLOCK context."""

    def __init__(self, command: str | None) -> None:
        self.command = command

    def dispatch(self, context: dict[str, Any]) -> None:
        if not self.command:
            raise LifecycleError("implementation/fix dispatcher command is unavailable")
        completed = subprocess.run(
            shlex.split(self.command),
            input=json.dumps(context),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise LifecycleError(
                f"implementation/fix dispatcher failed with exit {completed.returncode}"
                f"{': ' + detail if detail else ''}"
            )
