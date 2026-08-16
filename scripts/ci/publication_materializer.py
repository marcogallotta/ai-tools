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
import hashlib
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

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
from pr_lifecycle_owner import owning_task_identity_from_pr  # noqa: E402

REQUEST_MARKER = "dish-publication-materialize:v1"
MANIFEST_SCHEMA = "dish-publication-materialize-manifest-v1"
RESULT_MARKER = "dish-publication-materialize-result:v1"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_RE = re.compile(r"^\d{16}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MARKER_RE = re.compile(r"<!--\s*dish-publication-materialize:v1\s+(?P<fields>.*?)\s*-->", re.I | re.S)
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


class MaterializerError(RuntimeError):
    pass


def fail(message: str) -> "None":
    raise MaterializerError(message)


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
        fail(f"request comment must contain exactly one {REQUEST_MARKER} marker")
    fields = marker_fields(matches[0].group("fields"))
    required = {"request", "manifest", "manifest_sha256", "repository_id", "task", "pr", "branch", "head", "tree"}
    unknown = sorted(set(fields) - required)
    missing = sorted(required - set(fields))
    if missing:
        fail("materializer marker is missing fields: " + ", ".join(missing))
    if unknown:
        fail("materializer marker has unknown fields: " + ", ".join(unknown))
    branch = fields["branch"]
    if not branch or any(ch.isspace() for ch in branch) or branch.startswith("refs/"):
        fail("materializer branch must be a non-ref branch name without whitespace")
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
                raw = response.read()
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise MaterializerError(f"GitHub API {method} {path} failed HTTP {exc.code}: {body[:500]}") from exc
        try:
            return json.loads(raw or b"null")
        except json.JSONDecodeError as exc:
            raise MaterializerError(f"GitHub API {method} {path} returned invalid JSON") from exc

    @staticmethod
    def _repo_path(repository: str) -> str:
        return "/repos/" + "/".join(urlparse.quote(part, safe="") for part in repository.split("/"))

    def get_repository(self, repository: str) -> Mapping[str, Any]:
        return self.request("GET", self._repo_path(repository))

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


def _head_repo_identity(pr: Mapping[str, Any]) -> tuple[str, int]:
    head = pr.get("head")
    repo = head.get("repo") if isinstance(head, Mapping) else None
    if not isinstance(repo, Mapping):
        fail("PR head repository identity is missing")
    return str(repo.get("full_name") or ""), require_int(repo.get("id"), "PR head repository id", minimum=1)


def _duplicate_request_check(comments: Iterable[Mapping[str, Any]], current_id: int, request: RequestIdentity) -> None:
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

    repo_live = github.get_repository(repository)
    if require_int(repo_live.get("id"), "live repository id", minimum=1) != repository_id:
        fail("live repository id does not match event/request identity")
    if bool(repo_live.get("private")):
        fail("materializer V1 anonymous parent fetch requires the repository to remain public")
    default_branch = str(repo_live.get("default_branch") or "")
    if not default_branch:
        fail("live repository default branch is missing")
    if github.collaborator_permission(repository, commenter) not in WRITER_PERMISSIONS:
        fail("request commenter does not have repository write/maintain/admin permission")

    pr = github.get_pr(repository, request.pr_number)
    if str(pr.get("state")) != "open" or pr.get("draft") is not True:
        fail("materializer accepts only an existing open draft PR")
    base = pr.get("base")
    if not isinstance(base, Mapping) or str(base.get("ref") or "") != default_branch:
        fail("PR base must be the live repository default branch")
    head = pr.get("head")
    if not isinstance(head, Mapping):
        fail("PR head identity is missing")
    head_repo_name, head_repo_id = _head_repo_identity(pr)
    if head_repo_name != repository or head_repo_id != repository_id:
        fail("fork PRs are not eligible for exact-tree materialization")
    if str(head.get("ref") or "") != request.branch or require_sha(head.get("sha"), "PR head sha") != request.expected_old_head:
        fail("request branch/head does not match the exact live PR head")
    live_ref = github.get_ref(repository, request.branch)
    obj = live_ref.get("object") if isinstance(live_ref, Mapping) else None
    live_branch_head = require_sha(obj.get("sha") if isinstance(obj, Mapping) else None, "live branch head")
    if live_branch_head != request.expected_old_head:
        fail("live PR branch moved from the requested expected old head")
    body = str(pr.get("body") or "")
    if BLOCKER_HEADING not in body or BLOCKER_STATE_RE.search(body) is None:
        fail("PR does not contain the canonical LOCAL IMPLEMENTATION COMPLETION publication blocker")
    owner, owner_error = owning_task_identity_from_pr(pr)
    if owner_error or owner != request.task_gid:
        fail(f"PR owning-task identity does not match request task: {owner_error or owner!r}")

    _duplicate_request_check(github.list_issue_comments(repository, request.pr_number), comment_id, request)
    manifest_raw = github.get_blob_bytes(repository, request.manifest_blob)
    manifest = parse_manifest(manifest_raw, request, repository)
    return Admission(
        repository=repository,
        repository_id=repository_id,
        request=request,
        manifest=manifest,
        comment_id=comment_id,
        commenter=commenter,
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
    request_id: str
    task_gid: str
    pr_number: int
    branch: str
    parent: str
    tree: str
    candidate_commit: str
    changed_paths: tuple[str, ...]

    def json(self) -> dict[str, Any]:
        return {
            "schema": "dish-publication-materialize-result-v1",
            "request_id": self.request_id,
            "task_gid": self.task_gid,
            "pr_number": self.pr_number,
            "branch": self.branch,
            "parent": self.parent,
            "tree": self.tree,
            "candidate_commit": self.candidate_commit,
            "changed_paths": list(self.changed_paths),
        }


def materialize(admission: Admission, github: GitHubAPI, *, remote_url: str | None = None) -> MaterializationResult:
    patch = assemble_patch(admission.manifest, lambda sha: github.get_blob_bytes(admission.repository, sha))
    plan = reconstruct_tree(admission, patch, remote_url=remote_url)

    parent_commit = github.get_commit(admission.repository, admission.request.expected_old_head)
    tree_obj = parent_commit.get("tree") if isinstance(parent_commit, Mapping) else None
    parent_tree = require_sha(tree_obj.get("sha") if isinstance(tree_obj, Mapping) else None, "live parent tree")
    if parent_tree != plan.base_tree_sha:
        fail("GitHub parent tree does not match the fetched exact parent commit")

    api_entries: list[dict[str, Any]] = []
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
    candidate = github.create_commit(admission.repository, message, created_tree, admission.request.expected_old_head)
    created_commit = github.get_commit(admission.repository, candidate)
    parents = created_commit.get("parents") if isinstance(created_commit, Mapping) else None
    actual_parents = [require_sha(item.get("sha"), "created commit parent") for item in parents] if isinstance(parents, list) else []
    created_tree_obj = created_commit.get("tree") if isinstance(created_commit, Mapping) else None
    actual_tree = require_sha(created_tree_obj.get("sha") if isinstance(created_tree_obj, Mapping) else None, "created commit tree")
    if actual_parents != [admission.request.expected_old_head] or actual_tree != created_tree:
        fail("authoritative created-commit readback does not match exact parent/tree")
    return MaterializationResult(
        request_id=admission.request.request_id,
        task_gid=admission.request.task_gid,
        pr_number=admission.request.pr_number,
        branch=admission.request.branch,
        parent=admission.request.expected_old_head,
        tree=created_tree,
        candidate_commit=candidate,
        changed_paths=admission.manifest.changed_paths,
    )


def result_comment(result: MaterializationResult) -> str:
    value = result.json()
    digest = sha256_bytes(canonical_json(value))
    return (
        "Exact-tree publication candidate materialized. No branch ref was updated.\n\n"
        f"- PR: #{result.pr_number}\n- Task: `{result.task_gid}`\n- Branch: `{result.branch}`\n"
        f"- Expected parent: `{result.parent}`\n- Candidate commit: `{result.candidate_commit}`\n- Tree: `{result.tree}`\n\n"
        "Implementation must fetch/read back this candidate, verify exact parent/tree, then attach it to the existing "
        "PR branch with the separate connector expected-head/CAS ref update and re-read PR/branch/commit/tree state.\n\n"
        f"<!-- {RESULT_MARKER} request={result.request_id} candidate={result.candidate_commit} "
        f"parent={result.parent} tree={result.tree} digest={digest} -->"
    )


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


def command_filter(args: argparse.Namespace) -> int:
    try:
        event = _load_event(args.event_path)
        admission = admit_event(event, GitHubAPI())
    except MaterializerError as exc:
        _write_output(args.github_output, "valid", "false")
        _write_output(args.github_output, "reason_sha256", sha256_bytes(str(exc).encode("utf-8")))
        print(f"materializer request refused: {exc}", file=sys.stderr)
        return 0
    _write_output(args.github_output, "valid", "true")
    _write_output(args.github_output, "pr_number", str(admission.request.pr_number))
    _write_output(args.github_output, "request_id", admission.request.request_id)
    return 0


def command_materialize(args: argparse.Namespace) -> int:
    event = _load_event(args.event_path)
    github = GitHubAPI()
    admission = admit_event(event, github)
    result = materialize(admission, github)
    Path(args.result_path).write_text(json.dumps(result.json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_output(args.github_output, "candidate_commit", result.candidate_commit)
    _write_output(args.github_output, "tree", result.tree)
    _write_output(args.github_output, "parent", result.parent)
    _write_output(args.github_output, "pr_number", str(result.pr_number))
    _write_output(args.github_output, "request_id", result.request_id)
    return 0


def command_render_result(args: argparse.Namespace) -> int:
    value = json.loads(Path(args.result_path).read_text(encoding="utf-8"))
    result = MaterializationResult(
        request_id=require_uuid(value["request_id"], "request_id"),
        task_gid=require_task(value["task_gid"]),
        pr_number=require_int(value["pr_number"], "pr_number", minimum=1),
        branch=str(value["branch"]),
        parent=require_sha(value["parent"], "parent"),
        tree=require_sha(value["tree"], "tree"),
        candidate_commit=require_sha(value["candidate_commit"], "candidate_commit"),
        changed_paths=tuple(_safe_path(path) for path in value["changed_paths"]),
    )
    Path(args.output).write_text(result_comment(result), encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    filt = sub.add_parser("filter")
    filt.add_argument("--event-path", required=True)
    filt.add_argument("--github-output")
    mat = sub.add_parser("materialize")
    mat.add_argument("--event-path", required=True)
    mat.add_argument("--result-path", required=True)
    mat.add_argument("--github-output")
    render = sub.add_parser("render-result")
    render.add_argument("--result-path", required=True)
    render.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "filter":
            return command_filter(args)
        if args.command == "materialize":
            return command_materialize(args)
        return command_render_result(args)
    except MaterializerError as exc:
        print(f"publication-materializer: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
