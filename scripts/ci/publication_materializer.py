#!/usr/bin/env python3
"""Exact-tree GitHub publication materializer for bounded PR completion.

The transport is immutable Git blobs plus a manifest committed to one issue-comment
request. This helper validates the request from trusted default-branch code,
reconstructs the canonical binary/full-index patch without checking out candidate
code, proves the resulting Git tree, and creates only Git blob/tree/commit objects.
It intentionally has no reference-update, merge, review, PR-ready, or Asana write
primitive; attaching the returned child commit remains an Implementation action.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from enum import Enum
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import uuid
import zipfile

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
from pr_lifecycle_owner import materializer_owning_task_identity_from_pr  # noqa: E402

REQUEST_MARKER = "dish-publication-materialize:v1"
MANIFEST_SCHEMA = "dish-publication-materialize-manifest-v1"
RESULT_MARKER = "dish-publication-materialize-result:v1"
RESULT_SCHEMA = "dish-publication-materialize-result-v2"
RESULT_ARTIFACT_PREFIX = "dish-publication-materializer-result"
RESULT_FILENAME = "publication-materializer-result.json"
WORKFLOW_PATH = ".github/workflows/publication-materializer.yml"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_RE = re.compile(r"^\d{16}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MARKER_RE = re.compile(r"<!--\s*dish-publication-materialize:v1\s+(?P<fields>.*?)\s*-->", re.I | re.S)
RESULT_MARKER_RE = re.compile(r"<!--\s*dish-publication-materialize-result:v1\s+(?P<fields>.*?)\s*-->", re.I | re.S)
BLOCKER_HEADING = "## PUBLICATION BLOCKER — LOCAL BRANCH COMPLETION REQUIRED BEFORE REVIEW"
BLOCKER_STATE_RE = re.compile(r"(?im)^\s*State:\s*LOCAL IMPLEMENTATION COMPLETION REQUIRED\s*$")

# Connector transport limits are deliberately below GitHub's blob limits. The
# selected 8 KiB chunk size is large enough to keep ordinary multi-file PRs
# compact while leaving wide headroom for JSON/base64 connector envelopes.
MAX_CHUNK_BYTES = 8 * 1024
MAX_CHUNKS = 64
MAX_PATCH_BYTES = MAX_CHUNK_BYTES * MAX_CHUNKS
MAX_CHANGED_PATHS = 2048
ALLOWED_MODES = {"100644", "100755", "120000"}
WRITER_PERMISSIONS = {"write", "maintain", "admin"}


class Outcome(str, Enum):
    REQUEST_REPAIR_REQUIRED = "REQUEST_REPAIR_REQUIRED"
    REMOTE_PUBLICATION_UNAVAILABLE = "REMOTE_PUBLICATION_UNAVAILABLE"
    REMOTE_PUBLICATION_INELIGIBLE = "REMOTE_PUBLICATION_INELIGIBLE"
    SECURITY_OR_EXACTNESS_FAILURE = "SECURITY_OR_EXACTNESS_FAILURE"
    MATERIALIZED_RESULT_UNPUBLISHED = "MATERIALIZED_RESULT_UNPUBLISHED"
    UNRESOLVED_MATERIALIZED_RESULT = "UNRESOLVED_MATERIALIZED_RESULT"


class MaterializerError(RuntimeError):
    def __init__(self, message: str, outcome: Outcome = Outcome.SECURITY_OR_EXACTNESS_FAILURE) -> None:
        super().__init__(message)
        self.outcome = outcome


class GitHubAPIError(MaterializerError):
    def __init__(self, message: str, *, method: str, path: str, status: int) -> None:
        super().__init__(message, Outcome.SECURITY_OR_EXACTNESS_FAILURE)
        self.method = method
        self.path = path
        self.status = status


def fail(message: str, outcome: Outcome = Outcome.SECURITY_OR_EXACTNESS_FAILURE) -> "None":
    raise MaterializerError(message, outcome)


def repair_required(message: str) -> "None":
    fail(message, Outcome.REQUEST_REPAIR_REQUIRED)


def ineligible(message: str) -> "None":
    fail(message, Outcome.REMOTE_PUBLICATION_INELIGIBLE)


def unresolved_result(message: str) -> "None":
    fail(message, Outcome.UNRESOLVED_MATERIALIZED_RESULT)


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git_blob_sha(value: bytes) -> str:
    prefix = f"blob {len(value)}\0".encode("ascii")
    return hashlib.sha1(prefix + value).hexdigest()


def require_sha(value: Any, label: str) -> str:
    text = str(value or "").lower()
    if FULL_SHA_RE.fullmatch(text) is None:
        fail(f"{label} must be an exact lowercase 40-character Git SHA")
    return text


def require_digest(value: Any, label: str) -> str:
    text = str(value or "").lower()
    if DIGEST_RE.fullmatch(text) is None:
        fail(f"{label} must be an exact lowercase SHA-256 digest")
    return text


def require_uuid(value: Any, label: str) -> str:
    text = str(value or "").lower()
    if UUID_RE.fullmatch(text) is None:
        fail(f"{label} must be a UUID")
    return text


def require_task(value: Any) -> str:
    text = str(value or "")
    if TASK_RE.fullmatch(text) is None:
        fail("task must be a 16-digit Asana GID")
    return text


def require_int(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        fail(f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MaterializerError(f"{label} must be an integer") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        suffix = f"..{maximum}" if maximum is not None else f">={minimum}"
        fail(f"{label} must be in range {minimum}{suffix}")
    return parsed


def marker_fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in raw.split():
        if "=" not in token:
            fail("materializer marker contains a token without key=value")
        key, value = token.split("=", 1)
        key = key.strip().lower()
        if not key or key in fields:
            fail("materializer marker contains duplicate/empty field")
        fields[key] = value.strip()
    return fields


@dataclass(frozen=True)
class RequestIdentity:
    request_id: str
    manifest_blob: str
    manifest_sha256: str
    repository_id: int
    task_gid: str
    pr_number: int
    branch: str
    expected_old_head: str
    expected_final_tree: str


def parse_request_body(body: str) -> RequestIdentity:
    matches = list(MARKER_RE.finditer(body or ""))
    if len(matches) != 1:
        repair_required(f"request comment must contain exactly one {REQUEST_MARKER} marker")
    try:
        fields = marker_fields(matches[0].group("fields"))
    except MaterializerError as exc:
        raise MaterializerError(str(exc), Outcome.REQUEST_REPAIR_REQUIRED) from exc
    required = {"request", "manifest", "manifest_sha256", "repository_id", "task", "pr", "branch", "head", "tree"}
    unknown = sorted(set(fields) - required)
    missing = sorted(required - set(fields))
    if missing:
        repair_required("materializer marker is missing fields: " + ", ".join(missing))
    if unknown:
        repair_required("materializer marker has unknown fields: " + ", ".join(unknown))
    branch = fields["branch"]
    if not branch or any(ch.isspace() for ch in branch) or branch.startswith("refs/"):
        repair_required("materializer branch must be a non-ref branch name without whitespace")
    try:
        return RequestIdentity(
            request_id=require_uuid(fields["request"], "request"),
            manifest_blob=require_sha(fields["manifest"], "manifest"),
            manifest_sha256=require_digest(fields["manifest_sha256"], "manifest_sha256"),
            repository_id=require_int(fields["repository_id"], "repository_id", minimum=1),
            task_gid=require_task(fields["task"]),
            pr_number=require_int(fields["pr"], "pr", minimum=1),
            branch=branch,
            expected_old_head=require_sha(fields["head"], "head"),
            expected_final_tree=require_sha(fields["tree"], "tree"),
        )
    except MaterializerError as exc:
        raise MaterializerError(str(exc), Outcome.REQUEST_REPAIR_REQUIRED) from exc


def _safe_path(value: Any) -> str:
    text = str(value or "")
    path = PurePosixPath(text)
    if not text or text.startswith("/") or text.endswith("/") or "\0" in text:
        fail(f"invalid changed path: {text!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        fail(f"changed path escapes or is not normalized: {text!r}")
    normalized = path.as_posix()
    if normalized != text:
        fail(f"changed path is not canonical POSIX form: {text!r}")
    return text


@dataclass(frozen=True)
class Chunk:
    index: int
    blob_sha: str
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    request_id: str
    repository_full_name: str
    repository_id: int
    task_gid: str
    pr_number: int
    branch: str
    expected_old_head: str
    expected_final_tree: str
    changed_paths: tuple[str, ...]
    patch_byte_length: int
    patch_sha256: str
    chunks: tuple[Chunk, ...]


def parse_manifest(raw: bytes, request: RequestIdentity, repository_full_name: str) -> Manifest:
    if sha256_bytes(raw) != request.manifest_sha256:
        fail("manifest byte digest does not match request precommitment")
    if git_blob_sha(raw) != request.manifest_blob:
        fail("manifest Git blob SHA does not match request precommitment")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializerError("manifest blob is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        fail("manifest must be a JSON object")
    required = {
        "schema", "request_id", "repository", "task_gid", "pr_number", "branch",
        "expected_old_head", "expected_final_tree", "changed_paths", "patch", "chunks", "limits",
    }
    if set(value) != required:
        fail(f"manifest keys must be exactly {sorted(required)!r}")
    if value["schema"] != MANIFEST_SCHEMA:
        fail("unsupported materializer manifest schema")
    repo = value["repository"]
    if not isinstance(repo, dict) or set(repo) != {"full_name", "id"}:
        fail("manifest repository identity must contain exactly full_name/id")
    limits = value["limits"]
    expected_limits = {
        "max_chunk_bytes": MAX_CHUNK_BYTES,
        "max_chunks": MAX_CHUNKS,
        "max_patch_bytes": MAX_PATCH_BYTES,
        "max_changed_paths": MAX_CHANGED_PATHS,
    }
    if limits != expected_limits:
        fail("manifest transport limits do not match the trusted materializer limits")
    changed = value["changed_paths"]
    if not isinstance(changed, list) or not changed or len(changed) > MAX_CHANGED_PATHS:
        fail("manifest changed_paths must be a non-empty bounded list")
    changed_paths = tuple(_safe_path(path) for path in changed)
    if tuple(sorted(set(changed_paths))) != changed_paths:
        fail("manifest changed_paths must be unique and bytewise sorted")
    patch = value["patch"]
    if not isinstance(patch, dict) or set(patch) != {"byte_length", "sha256"}:
        fail("manifest patch must contain exactly byte_length/sha256")
    patch_length = require_int(patch["byte_length"], "patch.byte_length", minimum=1, maximum=MAX_PATCH_BYTES)
    patch_digest = require_digest(patch["sha256"], "patch.sha256")
    chunks_value = value["chunks"]
    if not isinstance(chunks_value, list) or not chunks_value or len(chunks_value) > MAX_CHUNKS:
        fail("manifest chunks must be a non-empty bounded list")
    chunks: list[Chunk] = []
    seen_blob_shas: set[str] = set()
    total = 0
    for expected_index, item in enumerate(chunks_value):
        if not isinstance(item, dict) or set(item) != {"index", "blob_sha", "byte_length", "sha256"}:
            fail("each chunk must contain exactly index/blob_sha/byte_length/sha256")
        index = require_int(item["index"], "chunk.index", minimum=0)
        if index != expected_index:
            fail("chunk indices must be contiguous and ordered from zero")
        blob_sha = require_sha(item["blob_sha"], "chunk.blob_sha")
        if blob_sha in seen_blob_shas:
            fail("duplicate chunk Git blob SHA is refused")
        seen_blob_shas.add(blob_sha)
        length = require_int(item["byte_length"], "chunk.byte_length", minimum=1, maximum=MAX_CHUNK_BYTES)
        digest = require_digest(item["sha256"], "chunk.sha256")
        total += length
        chunks.append(Chunk(index=index, blob_sha=blob_sha, byte_length=length, sha256=digest))
    if total != patch_length:
        fail("sum of chunk byte lengths does not equal patch.byte_length")

    manifest = Manifest(
        request_id=require_uuid(value["request_id"], "manifest request_id"),
        repository_full_name=str(repo["full_name"]),
        repository_id=require_int(repo["id"], "manifest repository.id", minimum=1),
        task_gid=require_task(value["task_gid"]),
        pr_number=require_int(value["pr_number"], "manifest pr_number", minimum=1),
        branch=str(value["branch"]),
        expected_old_head=require_sha(value["expected_old_head"], "manifest expected_old_head"),
        expected_final_tree=require_sha(value["expected_final_tree"], "manifest expected_final_tree"),
        changed_paths=changed_paths,
        patch_byte_length=patch_length,
        patch_sha256=patch_digest,
        chunks=tuple(chunks),
    )
    expected = (
        manifest.request_id == request.request_id
        and manifest.repository_full_name == repository_full_name
        and manifest.repository_id == request.repository_id
        and manifest.task_gid == request.task_gid
        and manifest.pr_number == request.pr_number
        and manifest.branch == request.branch
        and manifest.expected_old_head == request.expected_old_head
        and manifest.expected_final_tree == request.expected_final_tree
    )
    if not expected:
        fail("manifest identity does not exactly match the request marker/repository")
    return manifest


def decode_git_blob(payload: Mapping[str, Any], expected_sha: str) -> bytes:
    sha = require_sha(payload.get("sha"), "GitHub blob sha")
    if sha != expected_sha:
        fail("GitHub blob response SHA does not match requested object")
    if payload.get("encoding") != "base64":
        fail("GitHub blob response must use base64 encoding")
    try:
        raw = base64.b64decode(str(payload.get("content") or ""), validate=False)
    except (ValueError, TypeError) as exc:
        raise MaterializerError("GitHub blob response contains invalid base64") from exc
    if git_blob_sha(raw) != expected_sha:
        fail("GitHub blob bytes do not hash to their advertised Git blob SHA")
    return raw


def assemble_patch(manifest: Manifest, get_blob_bytes: Callable[[str], bytes]) -> bytes:
    pieces: list[bytes] = []
    for chunk in manifest.chunks:
        raw = get_blob_bytes(chunk.blob_sha)
        if len(raw) != chunk.byte_length:
            fail(f"chunk {chunk.index} byte length mismatch")
        if sha256_bytes(raw) != chunk.sha256:
            fail(f"chunk {chunk.index} SHA-256 mismatch")
        if git_blob_sha(raw) != chunk.blob_sha:
            fail(f"chunk {chunk.index} Git blob SHA mismatch")
        pieces.append(raw)
    patch = b"".join(pieces)
    if len(patch) != manifest.patch_byte_length:
        fail("reconstructed patch byte length mismatch")
    if sha256_bytes(patch) != manifest.patch_sha256:
        fail("reconstructed patch SHA-256 mismatch")
    return patch


class GitHubAPI:
    def __init__(self, token: str | None = None, api_root: str = "https://api.github.com") -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            fail("GITHUB_TOKEN is required")
        self.api_root = api_root.rstrip("/")

    def request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        raw = self.request_bytes(method, path, payload)
        try:
            return json.loads(raw or b"null")
        except json.JSONDecodeError as exc:
            raise MaterializerError(f"GitHub API {method} {path} returned invalid JSON") from exc

    def request_bytes(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> bytes:
        url = self.api_root + path
        data = None if payload is None else canonical_json(payload)
        req = urlrequest.Request(url, method=method, data=data)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=30) as response:
                return response.read()
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise GitHubAPIError(
                f"GitHub API {method} {path} failed HTTP {exc.code}: {body[:500]}",
                method=method,
                path=path,
                status=exc.code,
            ) from exc

    @staticmethod
    def _repo_path(repository: str) -> str:
        return "/repos/" + "/".join(urlparse.quote(part, safe="") for part in repository.split("/"))

    def get_repository(self, repository: str) -> Mapping[str, Any]:
        return self.request("GET", self._repo_path(repository))

    def get_authenticated_login(self) -> str:
        value = self.request("GET", "/user")
        login = str(value.get("login") or "") if isinstance(value, Mapping) else ""
        if not login:
            fail("authenticated GitHub login is missing")
        return login

    def get_pr(self, repository: str, number: int) -> Mapping[str, Any]:
        return self.request("GET", f"{self._repo_path(repository)}/pulls/{number}")

    def get_ref(self, repository: str, branch: str) -> Mapping[str, Any]:
        encoded = urlparse.quote(branch, safe="")
        return self.request("GET", f"{self._repo_path(repository)}/git/ref/heads/{encoded}")

    def collaborator_permission(self, repository: str, login: str) -> str:
        encoded = urlparse.quote(login, safe="")
        value = self.request("GET", f"{self._repo_path(repository)}/collaborators/{encoded}/permission")
        return str(value.get("permission") or "").lower()

    def get_blob_bytes(self, repository: str, sha: str) -> bytes:
        payload = self.request("GET", f"{self._repo_path(repository)}/git/blobs/{require_sha(sha, 'blob sha')}")
        return decode_git_blob(payload, sha)

    def list_issue_comments(self, repository: str, number: int) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        for page in range(1, 11):
            value = self.request("GET", f"{self._repo_path(repository)}/issues/{number}/comments?per_page=100&page={page}")
            if not isinstance(value, list):
                fail("GitHub issue comments response is not a list")
            out.extend(item for item in value if isinstance(item, Mapping))
            if len(value) < 100:
                return out
        fail("PR has more than 1000 comments; duplicate-request proof is intentionally bounded")

    def get_commit(self, repository: str, sha: str) -> Mapping[str, Any]:
        return self.request("GET", f"{self._repo_path(repository)}/git/commits/{require_sha(sha, 'commit sha')}")

    def get_artifact(self, repository: str, artifact_id: int) -> Mapping[str, Any]:
        return self.request("GET", f"{self._repo_path(repository)}/actions/artifacts/{require_int(artifact_id, 'artifact id', minimum=1)}")

    def list_artifacts(self, repository: str) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        for page in range(1, 11):
            value = self.request("GET", f"{self._repo_path(repository)}/actions/artifacts?per_page=100&page={page}")
            artifacts = value.get("artifacts") if isinstance(value, Mapping) else None
            if not isinstance(artifacts, list):
                fail("GitHub Actions artifacts response is malformed")
            out.extend(item for item in artifacts if isinstance(item, Mapping))
            if len(artifacts) < 100:
                return out
        unresolved_result("GitHub Actions artifact inventory exceeded the bounded recovery scan")

    def download_artifact_zip(self, repository: str, artifact_id: int) -> bytes:
        path = f"{self._repo_path(repository)}/actions/artifacts/{require_int(artifact_id, 'artifact id', minimum=1)}/zip"
        req = urlrequest.Request(self.api_root + path, method="GET")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")

        class _NoRedirect(urlrequest.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
                return None

        opener = urlrequest.build_opener(_NoRedirect)
        try:
            opener.open(req, timeout=30)
        except urlerror.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                body = exc.read().decode("utf-8", errors="replace")
                raise GitHubAPIError(
                    f"GitHub API GET {path} failed HTTP {exc.code}: {body[:500]}",
                    method="GET", path=path, status=exc.code,
                ) from exc
            location = str(exc.headers.get("Location") or "")
        else:
            fail("GitHub artifact download endpoint did not return the expected signed redirect")
        parsed = urlparse.urlsplit(location)
        if parsed.scheme != "https" or not parsed.netloc:
            fail("GitHub artifact download redirect is not a valid HTTPS URL")
        # Fetch the GitHub-supplied signed URL without forwarding GITHUB_TOKEN to
        # the external artifact storage host.
        try:
            with urlrequest.urlopen(urlrequest.Request(location, method="GET"), timeout=30) as response:
                return response.read()
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise GitHubAPIError(
                f"GitHub artifact signed download failed HTTP {exc.code}: {body[:500]}",
                method="GET", path=path, status=exc.code,
            ) from exc

    def get_workflow_run(self, repository: str, run_id: int) -> Mapping[str, Any]:
        return self.request("GET", f"{self._repo_path(repository)}/actions/runs/{require_int(run_id, 'run id', minimum=1)}")

    def create_issue_comment(self, repository: str, number: int, body: str) -> Mapping[str, Any]:
        return self.request(
            "POST",
            f"{self._repo_path(repository)}/issues/{require_int(number, 'PR number', minimum=1)}/comments",
            {"body": body},
        )

    def create_blob(self, repository: str, raw: bytes) -> str:
        value = self.request(
            "POST", f"{self._repo_path(repository)}/git/blobs",
            {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"},
        )
        return require_sha(value.get("sha"), "created blob sha")

    def create_tree(self, repository: str, base_tree: str, entries: list[dict[str, Any]]) -> str:
        value = self.request(
            "POST", f"{self._repo_path(repository)}/git/trees",
            {"base_tree": require_sha(base_tree, "base tree"), "tree": entries},
        )
        return require_sha(value.get("sha"), "created tree sha")

    def create_commit(self, repository: str, message: str, tree: str, parent: str) -> str:
        value = self.request(
            "POST", f"{self._repo_path(repository)}/git/commits",
            {"message": message, "tree": require_sha(tree, "tree"), "parents": [require_sha(parent, "parent")]},
        )
        return require_sha(value.get("sha"), "created commit sha")


@dataclass(frozen=True)
class Admission:
    repository: str
    repository_id: int
    request: RequestIdentity
    manifest: Manifest
    comment_id: int
    commenter: str
    prior_identical_comment_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AuthorPreflight:
    request_id: str
    repository: str
    repository_id: int
    task_gid: str
    pr_number: int
    branch: str
    expected_old_head: str
    expected_final_tree: str
    patch_byte_length: int
    changed_path_count: int


@dataclass(frozen=True)
class ResultArtifactEvidence:
    artifact_id: int
    name: str
    digest: str
    run_id: int
    expired: bool


def _normalize_artifact_digest(value: Any) -> str:
    text = str(value or "").lower()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    return require_digest(text, "artifact digest")


def result_artifact_name(request_id: str, run_id: int, run_attempt: int) -> str:
    return (
        f"{RESULT_ARTIFACT_PREFIX}-{require_uuid(request_id, 'request id')}-"
        f"run-{require_int(run_id, 'run id', minimum=1)}-attempt-{require_int(run_attempt, 'run attempt', minimum=1)}"
    )


def _artifact_request_prefix(request_id: str) -> str:
    return f"{RESULT_ARTIFACT_PREFIX}-{require_uuid(request_id, 'request id')}-run-"


def _matching_result_artifacts(
    github: GitHubAPI, repository: str, request_id: str
) -> tuple[ResultArtifactEvidence, ...]:
    prefix = _artifact_request_prefix(request_id)
    matches: list[ResultArtifactEvidence] = []
    for item in github.list_artifacts(repository):
        name = str(item.get("name") or "")
        if not name.startswith(prefix):
            continue
        workflow_run = item.get("workflow_run")
        if not isinstance(workflow_run, Mapping):
            unresolved_result("materializer result artifact is missing workflow-run identity")
        matches.append(
            ResultArtifactEvidence(
                artifact_id=require_int(item.get("id"), "artifact id", minimum=1),
                name=name,
                digest=_normalize_artifact_digest(item.get("digest")),
                run_id=require_int(workflow_run.get("id"), "artifact workflow run id", minimum=1),
                expired=bool(item.get("expired")),
            )
        )
    return tuple(sorted(matches, key=lambda item: item.artifact_id))


def resolve_result_artifact_for_request(
    github: GitHubAPI, repository: str, request_id: str
) -> ResultArtifactEvidence | None:
    matches = _matching_result_artifacts(github, repository, request_id)
    if len(matches) > 1:
        unresolved_result("duplicate materializer result artifacts exist for the same request UUID")
    if not matches:
        return None
    artifact = matches[0]
    if artifact.expired:
        unresolved_result("materializer result artifact for this request has expired")
    return artifact


def _head_repo_identity(pr: Mapping[str, Any]) -> tuple[str, int]:
    head = pr.get("head")
    repo = head.get("repo") if isinstance(head, Mapping) else None
    if not isinstance(repo, Mapping):
        fail("PR head repository identity is missing")
    return str(repo.get("full_name") or ""), require_int(repo.get("id"), "PR head repository id", minimum=1)


def _duplicate_request_check(
    comments: Iterable[Mapping[str, Any]], current_id: int, request: RequestIdentity
) -> tuple[int, ...]:
    prior_identical: list[int] = []
    for comment in comments:
        try:
            cid = int(comment.get("id"))
        except (TypeError, ValueError):
            continue
        if cid == current_id:
            continue
        body = str(comment.get("body") or "")
        if REQUEST_MARKER not in body:
            continue
        other = parse_request_body(body)
        if other.request_id != request.request_id:
            continue
        if other != request:
            fail("request UUID was already used with conflicting materializer identity")
        prior_identical.append(cid)
    return tuple(sorted(prior_identical))


def _validate_live_pr_admission(
    *,
    repository: str,
    repository_id: int,
    task_gid: str,
    pr_number: int,
    branch: str,
    expected_old_head: str,
    commenter: str,
    github: GitHubAPI,
) -> Mapping[str, Any]:
    repo_live = github.get_repository(repository)
    if require_int(repo_live.get("id"), "live repository id", minimum=1) != repository_id:
        fail("live repository id does not match request identity")
    if bool(repo_live.get("private")):
        ineligible("materializer V1 anonymous parent fetch requires the repository to remain public")
    default_branch = str(repo_live.get("default_branch") or "")
    if not default_branch:
        fail("live repository default branch is missing")
    if github.collaborator_permission(repository, commenter) not in WRITER_PERMISSIONS:
        fail("request commenter does not have repository write/maintain/admin permission")

    pr = github.get_pr(repository, pr_number)
    if str(pr.get("state")) != "open" or pr.get("draft") is not True:
        ineligible("materializer accepts only an existing open draft PR")
    base = pr.get("base")
    if not isinstance(base, Mapping) or str(base.get("ref") or "") != default_branch:
        ineligible("PR base must be the live repository default branch")
    head = pr.get("head")
    if not isinstance(head, Mapping):
        fail("PR head identity is missing")
    head_repo_name, head_repo_id = _head_repo_identity(pr)
    if head_repo_name != repository or head_repo_id != repository_id:
        ineligible("fork PRs are not eligible for exact-tree materialization")
    if str(head.get("ref") or "") != branch or require_sha(head.get("sha"), "PR head sha") != expected_old_head:
        fail("request branch/head does not match the exact live PR head")
    live_ref = github.get_ref(repository, branch)
    obj = live_ref.get("object") if isinstance(live_ref, Mapping) else None
    live_branch_head = require_sha(obj.get("sha") if isinstance(obj, Mapping) else None, "live branch head")
    if live_branch_head != expected_old_head:
        fail("live PR branch moved from the requested expected old head")
    body = str(pr.get("body") or "")
    if BLOCKER_HEADING not in body or BLOCKER_STATE_RE.search(body) is None:
        ineligible("PR does not contain the canonical LOCAL IMPLEMENTATION COMPLETION publication blocker")
    owner, owner_error, repairable = materializer_owning_task_identity_from_pr(pr)
    if owner_error:
        if repairable:
            repair_required(owner_error)
        fail(owner_error)
    if owner != task_gid:
        fail(f"canonical PR owning-task marker {owner!r} does not match request task {task_gid!r}")
    return pr


def author_preflight(preflight: AuthorPreflight, github: GitHubAPI) -> None:
    if preflight.patch_byte_length < 1 or preflight.patch_byte_length > MAX_PATCH_BYTES:
        ineligible(
            f"candidate patch size {preflight.patch_byte_length} exceeds materializer limit {MAX_PATCH_BYTES}"
        )
    if preflight.changed_path_count < 1 or preflight.changed_path_count > MAX_CHANGED_PATHS:
        ineligible(
            f"candidate changed-path count {preflight.changed_path_count} exceeds materializer limit {MAX_CHANGED_PATHS}"
        )
    actor = github.get_authenticated_login()
    _validate_live_pr_admission(
        repository=preflight.repository,
        repository_id=preflight.repository_id,
        task_gid=preflight.task_gid,
        pr_number=preflight.pr_number,
        branch=preflight.branch,
        expected_old_head=preflight.expected_old_head,
        commenter=actor,
        github=github,
    )
    for comment in github.list_issue_comments(preflight.repository, preflight.pr_number):
        body = str(comment.get("body") or "")
        if REQUEST_MARKER not in body:
            continue
        other = parse_request_body(body)
        if other.request_id != preflight.request_id:
            continue
        repair_required(
            "request UUID already exists on this PR; recover that request or use a fresh UUID for a genuinely new materialization"
        )
    if resolve_result_artifact_for_request(github, preflight.repository, preflight.request_id) is not None:
        unresolved_result(
            "durable materializer result already exists for this request UUID; rematerialization is forbidden"
        )


def admit_event(event: Mapping[str, Any], github: GitHubAPI) -> Admission:
    issue = event.get("issue")
    comment = event.get("comment")
    event_repo = event.get("repository")
    if not isinstance(issue, Mapping) or not isinstance(comment, Mapping) or not isinstance(event_repo, Mapping):
        fail("issue_comment event is missing issue/comment/repository identity")
    if not isinstance(issue.get("pull_request"), Mapping):
        fail("materializer requests are accepted only on pull requests")
    repository = str(event_repo.get("full_name") or "")
    if repository.count("/") != 1:
        fail("event repository full_name is invalid")
    repository_id = require_int(event_repo.get("id"), "event repository id", minimum=1)
    request = parse_request_body(str(comment.get("body") or ""))
    comment_id = require_int(comment.get("id"), "comment id", minimum=1)
    commenter = str((comment.get("user") or {}).get("login") or "") if isinstance(comment.get("user"), Mapping) else ""
    if not commenter:
        fail("request commenter login is missing")
    if request.repository_id != repository_id:
        fail("request repository id does not match issue_comment repository")
    issue_number = require_int(issue.get("number"), "issue number", minimum=1)
    if request.pr_number != issue_number:
        fail("request PR number does not match issue_comment PR")

    _validate_live_pr_admission(
        repository=repository,
        repository_id=repository_id,
        task_gid=request.task_gid,
        pr_number=request.pr_number,
        branch=request.branch,
        expected_old_head=request.expected_old_head,
        commenter=commenter,
        github=github,
    )

    prior_identical = _duplicate_request_check(
        github.list_issue_comments(repository, request.pr_number), comment_id, request
    )
    manifest_raw = github.get_blob_bytes(repository, request.manifest_blob)
    manifest = parse_manifest(manifest_raw, request, repository)
    return Admission(
        repository=repository,
        repository_id=repository_id,
        request=request,
        manifest=manifest,
        comment_id=comment_id,
        commenter=commenter,
        prior_identical_comment_ids=prior_identical,
    )


def _run_git(repo: Path, *args: str, env: Mapping[str, str] | None = None, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(repo), *args]
    actual = os.environ.copy()
    if env:
        actual.update(env)
    completed = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=actual, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")
        fail(f"trusted git command failed ({completed.returncode}): {' '.join(args)}\n{detail[:1000]}")
    return completed


def _index_entries(repo: Path, index: Path) -> dict[str, tuple[str, str]]:
    raw = _run_git(repo, "ls-files", "--stage", "-z", env={"GIT_INDEX_FILE": str(index)}).stdout
    entries: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, sep, path_bytes = record.partition(b"\t")
        if not sep:
            fail("git ls-files returned malformed index record")
        try:
            mode, sha, stage = meta.decode("ascii").split()
            path = path_bytes.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise MaterializerError("index contains an unsupported/non-UTF-8 path") from exc
        if stage != "0":
            fail("materializer index contains unresolved merge stages")
        entries[path] = (mode, require_sha(sha, "index blob sha"))
    return entries


def _changed_path_inventory(repo: Path, index: Path, parent: str) -> tuple[str, ...]:
    raw = _run_git(
        repo, "diff", "--cached", "--name-status", "-z", "--find-renames", "--no-ext-diff", "--no-textconv", parent,
        env={"GIT_INDEX_FILE": str(index)},
    ).stdout
    tokens = raw.split(b"\0")
    paths: set[str] = set()
    i = 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i].decode("ascii", errors="strict")
        i += 1
        count = 2 if status.startswith(("R", "C")) else 1
        for _ in range(count):
            if i >= len(tokens) or not tokens[i]:
                fail("git diff --name-status returned malformed path record")
            try:
                paths.add(_safe_path(tokens[i].decode("utf-8", errors="strict")))
            except UnicodeDecodeError as exc:
                raise MaterializerError("changed path is not valid UTF-8") from exc
            i += 1
    return tuple(sorted(paths))


@dataclass(frozen=True)
class TreePlan:
    tree_sha: str
    base_tree_sha: str
    entries: tuple[dict[str, Any], ...]
    blob_bytes: Mapping[str, bytes]


def reconstruct_tree(admission: Admission, patch: bytes, *, remote_url: str | None = None) -> TreePlan:
    parent = admission.request.expected_old_head
    manifest = admission.manifest
    with tempfile.TemporaryDirectory(prefix="dish-publication-materializer-") as td:
        root = Path(td) / "repo"
        root.mkdir()
        _run_git(root, "init", "-q")
        remote = remote_url or f"https://github.com/{admission.repository}.git"
        _run_git(root, "fetch", "-q", "--no-tags", "--depth=1", remote, parent)
        index = Path(td) / "index"
        env = {"GIT_INDEX_FILE": str(index)}
        _run_git(root, "read-tree", parent, env=env)
        base_entries = _index_entries(root, index)
        patch_path = Path(td) / "candidate.patch"
        patch_path.write_bytes(patch)
        _run_git(root, "apply", "--cached", "--binary", "--whitespace=nowarn", str(patch_path), env=env)
        inventory = _changed_path_inventory(root, index, parent)
        if inventory != manifest.changed_paths:
            fail(f"applied patch changed paths {inventory!r}, expected {manifest.changed_paths!r}")
        final_entries = _index_entries(root, index)
        for path in inventory:
            for entries in (base_entries, final_entries):
                item = entries.get(path)
                if item is not None and item[0] == "160000":
                    fail("gitlinks/submodules are not supported by publication materializer V1")
                if item is not None and item[0] not in ALLOWED_MODES:
                    fail(f"unsupported Git mode {item[0]} for {path}")
        tree_sha = require_sha(
            _run_git(root, "write-tree", env=env).stdout.decode("ascii").strip(),
            "reconstructed tree sha",
        )
        if tree_sha != manifest.expected_final_tree:
            fail(f"reconstructed tree {tree_sha} != precommitted expected tree {manifest.expected_final_tree}")
        parent_commit = _run_git(root, "cat-file", "-p", parent).stdout.decode("utf-8", errors="replace")
        first = parent_commit.splitlines()[0] if parent_commit else ""
        if not first.startswith("tree "):
            fail("parent commit does not expose a tree")
        base_tree = require_sha(first.split()[1], "parent tree sha")
        api_entries: list[dict[str, Any]] = []
        blobs: dict[str, bytes] = {}
        for path in inventory:
            final = final_entries.get(path)
            if final is None:
                base = base_entries.get(path)
                if base is None:
                    fail(f"changed path {path} is absent from both parent and final index")
                api_entries.append({"path": path, "mode": base[0], "type": "blob", "sha": None})
                continue
            mode, sha = final
            raw = _run_git(root, "cat-file", "blob", sha).stdout
            if git_blob_sha(raw) != sha:
                fail(f"local reconstructed blob bytes do not match index SHA for {path}")
            blobs[path] = raw
            api_entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
        return TreePlan(tree_sha=tree_sha, base_tree_sha=base_tree, entries=tuple(api_entries), blob_bytes=blobs)


@dataclass(frozen=True)
class MaterializationResult:
    repository: str
    repository_id: int
    request_id: str
    task_gid: str
    pr_number: int
    branch: str
    expected_old_head: str
    expected_parent: str
    expected_final_tree: str
    candidate_commit: str
    changed_paths: tuple[str, ...]
    workflow_path: str
    source_sha: str
    run_id: int
    run_attempt: int

    @property
    def parent(self) -> str:
        return self.expected_parent

    @property
    def tree(self) -> str:
        return self.expected_final_tree

    @property
    def artifact_name(self) -> str:
        return result_artifact_name(self.request_id, self.run_id, self.run_attempt)

    def json(self) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "repository": {"full_name": self.repository, "id": self.repository_id},
            "request_id": self.request_id,
            "task_gid": self.task_gid,
            "pr_number": self.pr_number,
            "branch": self.branch,
            "expected_old_head": self.expected_old_head,
            "expected_parent": self.expected_parent,
            "expected_final_tree": self.expected_final_tree,
            "candidate_commit": self.candidate_commit,
            "changed_paths": list(self.changed_paths),
            "workflow": {
                "path": self.workflow_path,
                "source_sha": self.source_sha,
                "run_id": self.run_id,
                "run_attempt": self.run_attempt,
            },
        }


def parse_materialization_result(value: Mapping[str, Any]) -> MaterializationResult:
    required = {
        "schema", "repository", "request_id", "task_gid", "pr_number", "branch",
        "expected_old_head", "expected_parent", "expected_final_tree", "candidate_commit",
        "changed_paths", "workflow",
    }
    if set(value) != required:
        unresolved_result(f"materializer result keys must be exactly {sorted(required)!r}")
    if value.get("schema") != RESULT_SCHEMA:
        unresolved_result("unsupported materializer result schema")
    repository = value.get("repository")
    workflow = value.get("workflow")
    changed = value.get("changed_paths")
    if not isinstance(repository, Mapping) or set(repository) != {"full_name", "id"}:
        unresolved_result("materializer result repository identity is malformed")
    if not isinstance(workflow, Mapping) or set(workflow) != {"path", "source_sha", "run_id", "run_attempt"}:
        unresolved_result("materializer result workflow identity is malformed")
    if not isinstance(changed, list) or not changed or len(changed) > MAX_CHANGED_PATHS:
        unresolved_result("materializer result changed_paths is malformed")
    try:
        changed_paths = tuple(_safe_path(path) for path in changed)
        result = MaterializationResult(
            repository=str(repository.get("full_name") or ""),
            repository_id=require_int(repository.get("id"), "result repository id", minimum=1),
            request_id=require_uuid(value.get("request_id"), "result request id"),
            task_gid=require_task(value.get("task_gid")),
            pr_number=require_int(value.get("pr_number"), "result pr number", minimum=1),
            branch=str(value.get("branch") or ""),
            expected_old_head=require_sha(value.get("expected_old_head"), "result expected old head"),
            expected_parent=require_sha(value.get("expected_parent"), "result expected parent"),
            expected_final_tree=require_sha(value.get("expected_final_tree"), "result expected final tree"),
            candidate_commit=require_sha(value.get("candidate_commit"), "result candidate commit"),
            changed_paths=changed_paths,
            workflow_path=str(workflow.get("path") or ""),
            source_sha=require_sha(workflow.get("source_sha"), "result workflow source sha"),
            run_id=require_int(workflow.get("run_id"), "result workflow run id", minimum=1),
            run_attempt=require_int(workflow.get("run_attempt"), "result workflow run attempt", minimum=1),
        )
    except MaterializerError as exc:
        raise MaterializerError(str(exc), Outcome.UNRESOLVED_MATERIALIZED_RESULT) from exc
    if result.repository.count("/") != 1 or not result.branch or any(ch.isspace() for ch in result.branch):
        unresolved_result("materializer result repository/branch identity is malformed")
    if result.expected_old_head != result.expected_parent:
        unresolved_result("materializer result old-head/parent identities conflict")
    if result.workflow_path != WORKFLOW_PATH:
        unresolved_result("materializer result workflow path does not name the trusted materializer workflow")
    if tuple(sorted(set(result.changed_paths))) != result.changed_paths:
        unresolved_result("materializer result changed_paths must be unique and sorted")
    return result


def _remote_publication_error(exc: GitHubAPIError) -> MaterializerError:
    if exc.status in {403, 408, 429} or exc.status >= 500:
        return MaterializerError(str(exc), Outcome.REMOTE_PUBLICATION_UNAVAILABLE)
    return exc


def materialize(
    admission: Admission,
    github: GitHubAPI,
    *,
    source_sha: str = "0" * 40,
    run_id: int = 1,
    run_attempt: int = 1,
    remote_url: str | None = None,
) -> MaterializationResult:
    source_sha = require_sha(source_sha, "trusted materializer source sha")
    run_id = require_int(run_id, "workflow run id", minimum=1)
    run_attempt = require_int(run_attempt, "workflow run attempt", minimum=1)
    patch = assemble_patch(admission.manifest, lambda sha: github.get_blob_bytes(admission.repository, sha))
    plan = reconstruct_tree(admission, patch, remote_url=remote_url)

    parent_commit = github.get_commit(admission.repository, admission.request.expected_old_head)
    tree_obj = parent_commit.get("tree") if isinstance(parent_commit, Mapping) else None
    parent_tree = require_sha(tree_obj.get("sha") if isinstance(tree_obj, Mapping) else None, "live parent tree")
    if parent_tree != plan.base_tree_sha:
        fail("GitHub parent tree does not match the fetched exact parent commit")

    api_entries: list[dict[str, Any]] = []
    try:
        for entry in plan.entries:
            current = dict(entry)
            path = str(current["path"])
            if current["sha"] is not None:
                created = github.create_blob(admission.repository, plan.blob_bytes[path])
                if created != current["sha"]:
                    fail(f"GitHub created blob SHA mismatch for {path}")
                current["sha"] = created
            api_entries.append(current)
        created_tree = github.create_tree(admission.repository, parent_tree, api_entries)
        if created_tree != admission.manifest.expected_final_tree:
            fail("GitHub-created tree does not match the precommitted expected final tree")
        message = (
            f"Publication materialization {admission.request.request_id}\n\n"
            f"Asana-Task: {admission.request.task_gid}\n"
            f"Expected-Parent: {admission.request.expected_old_head}\n"
            f"Expected-Tree: {created_tree}\n"
        )
        candidate = github.create_commit(
            admission.repository, message, created_tree, admission.request.expected_old_head
        )
    except GitHubAPIError as exc:
        raise _remote_publication_error(exc) from exc

    created_commit = github.get_commit(admission.repository, candidate)
    parents = created_commit.get("parents") if isinstance(created_commit, Mapping) else None
    actual_parents = [
        require_sha(item.get("sha"), "created commit parent")
        for item in parents
        if isinstance(item, Mapping)
    ] if isinstance(parents, list) else []
    created_tree_obj = created_commit.get("tree") if isinstance(created_commit, Mapping) else None
    actual_tree = require_sha(
        created_tree_obj.get("sha") if isinstance(created_tree_obj, Mapping) else None,
        "created commit tree",
    )
    if actual_parents != [admission.request.expected_old_head] or actual_tree != created_tree:
        fail("authoritative created-commit readback does not match exact parent/tree")
    return MaterializationResult(
        repository=admission.repository,
        repository_id=admission.repository_id,
        request_id=admission.request.request_id,
        task_gid=admission.request.task_gid,
        pr_number=admission.request.pr_number,
        branch=admission.request.branch,
        expected_old_head=admission.request.expected_old_head,
        expected_parent=admission.request.expected_old_head,
        expected_final_tree=created_tree,
        candidate_commit=candidate,
        changed_paths=admission.manifest.changed_paths,
        workflow_path=WORKFLOW_PATH,
        source_sha=source_sha,
        run_id=run_id,
        run_attempt=run_attempt,
    )


def _artifact_evidence_from_live(value: Mapping[str, Any]) -> ResultArtifactEvidence:
    workflow_run = value.get("workflow_run")
    if not isinstance(workflow_run, Mapping):
        unresolved_result("materializer result artifact is missing workflow-run identity")
    return ResultArtifactEvidence(
        artifact_id=require_int(value.get("id"), "artifact id", minimum=1),
        name=str(value.get("name") or ""),
        digest=_normalize_artifact_digest(value.get("digest")),
        run_id=require_int(workflow_run.get("id"), "artifact workflow run id", minimum=1),
        expired=bool(value.get("expired")),
    )


def load_result_artifact(
    github: GitHubAPI,
    repository: str,
    artifact: ResultArtifactEvidence,
) -> MaterializationResult:
    try:
        live = github.get_artifact(repository, artifact.artifact_id)
        live_evidence = _artifact_evidence_from_live(live)
        if live_evidence != artifact:
            unresolved_result("materializer result artifact metadata changed or mismatches recovery preflight")
        if live_evidence.expired:
            unresolved_result("materializer result artifact has expired")
        archive = github.download_artifact_zip(repository, artifact.artifact_id)
    except GitHubAPIError as exc:
        raise MaterializerError(str(exc), Outcome.UNRESOLVED_MATERIALIZED_RESULT) from exc
    if sha256_bytes(archive) != artifact.digest:
        unresolved_result("materializer result artifact archive digest mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            names = [info.filename for info in zf.infolist() if not info.is_dir()]
            if names != [RESULT_FILENAME]:
                unresolved_result("materializer result artifact must contain exactly the canonical result JSON")
            raw = zf.read(RESULT_FILENAME)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise MaterializerError(
            "materializer result artifact is corrupt or unreadable",
            Outcome.UNRESOLVED_MATERIALIZED_RESULT,
        ) from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializerError(
            "materializer result artifact JSON is corrupt",
            Outcome.UNRESOLVED_MATERIALIZED_RESULT,
        ) from exc
    if not isinstance(value, Mapping):
        unresolved_result("materializer result artifact JSON must be an object")
    return parse_materialization_result(value)


def verify_recovered_result(
    admission: Admission,
    result: MaterializationResult,
    artifact: ResultArtifactEvidence,
    github: GitHubAPI,
    *,
    require_current_run_attempt: bool,
) -> None:
    expected = (
        result.repository == admission.repository
        and result.repository_id == admission.repository_id
        and result.request_id == admission.request.request_id
        and result.task_gid == admission.request.task_gid
        and result.pr_number == admission.request.pr_number
        and result.branch == admission.request.branch
        and result.expected_old_head == admission.request.expected_old_head
        and result.expected_parent == admission.request.expected_old_head
        and result.expected_final_tree == admission.request.expected_final_tree
        and result.changed_paths == admission.manifest.changed_paths
    )
    if not expected:
        unresolved_result("durable materializer result identity does not exactly match the live admitted request")
    if artifact.name != result.artifact_name:
        unresolved_result("materializer result artifact name does not match request/run-attempt identity")
    if artifact.run_id != result.run_id:
        unresolved_result("materializer result artifact workflow-run identity conflicts with result JSON")
    run = github.get_workflow_run(admission.repository, result.run_id)
    run_attempt = require_int(run.get("run_attempt"), "live workflow run attempt", minimum=1)
    if require_current_run_attempt and run_attempt != result.run_attempt:
        unresolved_result("stale prior workflow-run-attempt result cannot satisfy current recovery")
    if str(run.get("event") or "") != "issue_comment":
        unresolved_result("materializer result workflow run did not originate from issue_comment")
    repo = run.get("repository")
    if isinstance(repo, Mapping) and require_int(repo.get("id"), "workflow repository id", minimum=1) != admission.repository_id:
        unresolved_result("materializer result workflow run repository identity mismatches request")
    live_repo = github.get_repository(admission.repository)
    default_branch = str(live_repo.get("default_branch") or "")
    if not default_branch or str(run.get("head_branch") or "") != default_branch:
        unresolved_result("materializer result workflow run is not bound to the repository default branch")
    if require_sha(run.get("head_sha"), "workflow run head sha") != result.source_sha:
        unresolved_result("materializer result source SHA does not match the trusted workflow-run head")
    if str(run.get("path") or "") != WORKFLOW_PATH:
        unresolved_result("materializer result workflow run path does not match the trusted workflow")
    source = github.get_commit(admission.repository, result.source_sha)
    if not isinstance(source, Mapping):
        unresolved_result("trusted materializer workflow source commit cannot be read")
    candidate = github.get_commit(admission.repository, result.candidate_commit)
    parents = candidate.get("parents") if isinstance(candidate, Mapping) else None
    actual_parents = [
        require_sha(item.get("sha"), "recovered candidate parent")
        for item in parents
        if isinstance(item, Mapping)
    ] if isinstance(parents, list) else []
    tree_obj = candidate.get("tree") if isinstance(candidate, Mapping) else None
    actual_tree = require_sha(
        tree_obj.get("sha") if isinstance(tree_obj, Mapping) else None,
        "recovered candidate tree",
    )
    if actual_parents != [result.expected_parent] or actual_tree != result.expected_final_tree:
        unresolved_result("recovered candidate does not independently prove the durable expected parent/tree")


def recover_result(
    admission: Admission,
    github: GitHubAPI,
    artifact: ResultArtifactEvidence,
    *,
    require_current_run_attempt: bool = False,
) -> MaterializationResult:
    result = load_result_artifact(github, admission.repository, artifact)
    verify_recovered_result(
        admission,
        result,
        artifact,
        github,
        require_current_run_attempt=require_current_run_attempt,
    )
    return result


def choose_request_route(
    admission: Admission,
    github: GitHubAPI,
    *,
    current_run_attempt: int,
) -> tuple[str, ResultArtifactEvidence | None]:
    current_run_attempt = require_int(current_run_attempt, "current workflow run attempt", minimum=1)
    artifact = resolve_result_artifact_for_request(github, admission.repository, admission.request.request_id)
    if artifact is not None:
        return "recover", artifact
    if admission.prior_identical_comment_ids or current_run_attempt > 1:
        unresolved_result(
            "this request is recovery-only but no unique live durable result exists; rematerialization is forbidden"
        )
    return "materialize", None


def result_comment(result: MaterializationResult) -> str:
    value = result.json()
    digest = sha256_bytes(canonical_json(value))
    return (
        "Exact-tree publication candidate materialized and durably recovered. No branch ref was updated.\n\n"
        f"- PR: #{result.pr_number}\n- Task: `{result.task_gid}`\n- Branch: `{result.branch}`\n"
        f"- Expected parent: `{result.parent}`\n- Candidate commit: `{result.candidate_commit}`\n- Tree: `{result.tree}`\n"
        f"- Durable result: `{result.artifact_name}` (workflow run `{result.run_id}`, attempt `{result.run_attempt}`)\n\n"
        "This result is locator/recovery evidence only. It grants no branch/ref movement, Review, Integration, "
        "ready-for-review, Asana, or runtime authority. Implementation must independently revalidate live branch/PR "
        "authority before any separate candidate attachment.\n\n"
        f"<!-- {RESULT_MARKER} request={result.request_id} candidate={result.candidate_commit} "
        f"parent={result.parent} tree={result.tree} digest={digest} -->"
    )


def _published_result_match(body: str, result: MaterializationResult) -> bool | None:
    matches = list(RESULT_MARKER_RE.finditer(body or ""))
    if not matches:
        return None
    if len(matches) != 1:
        fail("result comment contains duplicate materializer result markers")
    fields = marker_fields(matches[0].group("fields"))
    required = {"request", "candidate", "parent", "tree", "digest"}
    if set(fields) != required:
        fail("materializer result marker fields are malformed")
    if require_uuid(fields["request"], "result marker request") != result.request_id:
        return None
    expected_digest = sha256_bytes(canonical_json(result.json()))
    expected = {
        "request": result.request_id,
        "candidate": result.candidate_commit,
        "parent": result.parent,
        "tree": result.tree,
        "digest": expected_digest,
    }
    if fields != expected:
        fail("existing result publication for this request conflicts with the recovered durable result")
    return True


def publish_result(
    admission: Admission,
    result: MaterializationResult,
    github: GitHubAPI,
) -> int:
    matching_ids: list[int] = []
    for comment in github.list_issue_comments(admission.repository, result.pr_number):
        body = str(comment.get("body") or "")
        if RESULT_MARKER not in body:
            continue
        if _published_result_match(body, result):
            matching_ids.append(require_int(comment.get("id"), "result comment id", minimum=1))
    if len(matching_ids) > 1:
        fail("duplicate matching result publications exist for this request")
    if matching_ids:
        return matching_ids[0]
    body = result_comment(result)
    try:
        created = github.create_issue_comment(admission.repository, result.pr_number, body)
    except GitHubAPIError as exc:
        raise MaterializerError(str(exc), Outcome.MATERIALIZED_RESULT_UNPUBLISHED) from exc
    created_id = require_int(created.get("id"), "created result comment id", minimum=1)
    created_body = str(created.get("body") or "")
    if _published_result_match(created_body, result) is not True:
        fail("created result comment readback does not match durable result")
    return created_id


def _load_event(path: str) -> Mapping[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializerError(f"cannot read issue_comment event: {exc}") from exc
    if not isinstance(value, Mapping):
        fail("issue_comment event must be a JSON object")
    return value


def _write_output(path: str | None, key: str, value: str) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def _artifact_from_args(args: argparse.Namespace) -> ResultArtifactEvidence:
    return ResultArtifactEvidence(
        artifact_id=require_int(args.artifact_id, "artifact id", minimum=1),
        name=str(args.artifact_name),
        digest=_normalize_artifact_digest(args.artifact_digest),
        run_id=require_int(args.artifact_run_id, "artifact run id", minimum=1),
        expired=False,
    )


def command_author_preflight(args: argparse.Namespace) -> int:
    preflight = AuthorPreflight(
        request_id=require_uuid(args.request_id, "request id"),
        repository=str(args.repository),
        repository_id=require_int(args.repository_id, "repository id", minimum=1),
        task_gid=require_task(args.task),
        pr_number=require_int(args.pr_number, "PR number", minimum=1),
        branch=str(args.branch),
        expected_old_head=require_sha(args.head, "expected old head"),
        expected_final_tree=require_sha(args.tree, "expected final tree"),
        patch_byte_length=require_int(args.patch_byte_length, "patch byte length", minimum=1),
        changed_path_count=require_int(args.changed_path_count, "changed path count", minimum=1),
    )
    author_preflight(preflight, GitHubAPI())
    _write_output(args.github_output, "valid", "true")
    _write_output(args.github_output, "classification", "ADMISSION_PREFLIGHT_PASSED")
    return 0


def command_filter(args: argparse.Namespace) -> int:
    try:
        event = _load_event(args.event_path)
        github = GitHubAPI()
        admission = admit_event(event, github)
        route, artifact = choose_request_route(
            admission,
            github,
            current_run_attempt=require_int(args.run_attempt, "workflow run attempt", minimum=1),
        )
    except MaterializerError as exc:
        _write_output(args.github_output, "valid", "false")
        _write_output(args.github_output, "classification", exc.outcome.value)
        _write_output(args.github_output, "reason_sha256", sha256_bytes(str(exc).encode("utf-8")))
        print(f"materializer request refused: {exc.outcome.value}: {exc}", file=sys.stderr)
        return 0
    _write_output(args.github_output, "valid", "true")
    _write_output(args.github_output, "classification", "RECOVERY_REQUIRED" if route == "recover" else "MATERIALIZATION_ADMITTED")
    _write_output(args.github_output, "route", route)
    _write_output(args.github_output, "pr_number", str(admission.request.pr_number))
    _write_output(args.github_output, "request_id", admission.request.request_id)
    if artifact is not None:
        _write_output(args.github_output, "artifact_id", str(artifact.artifact_id))
        _write_output(args.github_output, "artifact_name", artifact.name)
        _write_output(args.github_output, "artifact_digest", artifact.digest)
        _write_output(args.github_output, "artifact_run_id", str(artifact.run_id))
    return 0


def command_materialize(args: argparse.Namespace) -> int:
    event = _load_event(args.event_path)
    github = GitHubAPI()
    admission = admit_event(event, github)
    route, artifact = choose_request_route(
        admission,
        github,
        current_run_attempt=require_int(args.run_attempt, "workflow run attempt", minimum=1),
    )
    if route != "materialize" or artifact is not None:
        unresolved_result("a durable result already exists or recovery is required; rematerialization is forbidden")
    result = materialize(
        admission,
        github,
        source_sha=args.source_sha,
        run_id=require_int(args.run_id, "workflow run id", minimum=1),
        run_attempt=require_int(args.run_attempt, "workflow run attempt", minimum=1),
    )
    Path(args.result_path).write_text(
        json.dumps(result.json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_output(args.github_output, "candidate_commit", result.candidate_commit)
    _write_output(args.github_output, "tree", result.tree)
    _write_output(args.github_output, "parent", result.parent)
    _write_output(args.github_output, "pr_number", str(result.pr_number))
    _write_output(args.github_output, "request_id", result.request_id)
    _write_output(args.github_output, "artifact_name", result.artifact_name)
    return 0


def command_verify_result_artifact(args: argparse.Namespace) -> int:
    event = _load_event(args.event_path)
    github = GitHubAPI()
    admission = admit_event(event, github)
    artifact = _artifact_from_args(args)
    result = recover_result(admission, github, artifact, require_current_run_attempt=True)
    _write_output(args.github_output, "candidate_commit", result.candidate_commit)
    _write_output(args.github_output, "parent", result.parent)
    _write_output(args.github_output, "tree", result.tree)
    _write_output(args.github_output, "request_id", result.request_id)
    return 0


def command_publish_result(args: argparse.Namespace) -> int:
    event = _load_event(args.event_path)
    github = GitHubAPI()
    admission = admit_event(event, github)
    artifact = _artifact_from_args(args)
    result = recover_result(admission, github, artifact, require_current_run_attempt=True)
    comment_id = publish_result(admission, result, github)
    _write_output(args.github_output, "comment_id", str(comment_id))
    _write_output(args.github_output, "candidate_commit", result.candidate_commit)
    _write_output(args.github_output, "classification", "RESULT_PUBLISHED")
    return 0


def command_render_result(args: argparse.Namespace) -> int:
    try:
        value = json.loads(Path(args.result_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializerError("cannot read materializer result JSON") from exc
    if not isinstance(value, Mapping):
        unresolved_result("materializer result JSON must be an object")
    result = parse_materialization_result(value)
    Path(args.output).write_text(result_comment(result), encoding="utf-8")
    return 0


def _add_artifact_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--artifact-digest", required=True)
    parser.add_argument("--artifact-run-id", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("author-preflight")
    pre.add_argument("--request-id", required=True)
    pre.add_argument("--repository", required=True)
    pre.add_argument("--repository-id", required=True)
    pre.add_argument("--task", required=True)
    pre.add_argument("--pr-number", required=True)
    pre.add_argument("--branch", required=True)
    pre.add_argument("--head", required=True)
    pre.add_argument("--tree", required=True)
    pre.add_argument("--patch-byte-length", required=True)
    pre.add_argument("--changed-path-count", required=True)
    pre.add_argument("--github-output")

    filt = sub.add_parser("filter")
    filt.add_argument("--event-path", required=True)
    filt.add_argument("--run-attempt", required=True)
    filt.add_argument("--github-output")

    mat = sub.add_parser("materialize")
    mat.add_argument("--event-path", required=True)
    mat.add_argument("--result-path", required=True)
    mat.add_argument("--source-sha", required=True)
    mat.add_argument("--run-id", required=True)
    mat.add_argument("--run-attempt", required=True)
    mat.add_argument("--github-output")

    verify = sub.add_parser("verify-result-artifact")
    verify.add_argument("--event-path", required=True)
    _add_artifact_args(verify)
    verify.add_argument("--github-output")

    publish = sub.add_parser("publish-result")
    publish.add_argument("--event-path", required=True)
    _add_artifact_args(publish)
    publish.add_argument("--github-output")

    render = sub.add_parser("render-result")
    render.add_argument("--result-path", required=True)
    render.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "author-preflight":
            return command_author_preflight(args)
        if args.command == "filter":
            return command_filter(args)
        if args.command == "materialize":
            return command_materialize(args)
        if args.command == "verify-result-artifact":
            return command_verify_result_artifact(args)
        if args.command == "publish-result":
            return command_publish_result(args)
        return command_render_result(args)
    except MaterializerError as exc:
        output_path = getattr(args, "github_output", None)
        _write_output(output_path, "valid", "false")
        _write_output(output_path, "classification", exc.outcome.value)
        _write_output(output_path, "reason_sha256", sha256_bytes(str(exc).encode("utf-8")))
        print(f"publication-materializer: {exc.outcome.value}: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
