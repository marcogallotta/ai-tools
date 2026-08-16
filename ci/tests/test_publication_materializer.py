from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "ci" / "publication_materializer.py"
spec = importlib.util.spec_from_file_location("publication_materializer", MODULE_PATH)
pm = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = pm
spec.loader.exec_module(pm)


def git(repo: Path, *args: str, text: bool = True):
    cp = subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return cp.stdout.decode().strip() if text else cp.stdout


def changed_inventory(repo: Path, base: str, candidate: str) -> tuple[str, ...]:
    raw = git(repo, "diff", "--name-status", "-z", "--find-renames", base, candidate, text=False)
    tokens = raw.split(b"\0")
    paths: set[str] = set()
    i = 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i].decode("ascii")
        i += 1
        count = 2 if status.startswith(("R", "C")) else 1
        for _ in range(count):
            paths.add(tokens[i].decode("utf-8"))
            i += 1
    return tuple(sorted(paths))


@pytest.fixture()
def candidate_repo(tmp_path: Path):
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "dish-agent@local.invalid")
    git(repo, "config", "user.name", "Dish Agent Test")
    (repo / "edit.txt").write_text("before\n", encoding="utf-8")
    (repo / "delete.txt").write_text("delete\n", encoding="utf-8")
    (repo / "rename-old.txt").write_text("rename me\n", encoding="utf-8")
    (repo / "script.sh").write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"\x00\x01old\xff")
    (repo / "target-a").write_text("a\n", encoding="utf-8")
    (repo / "target-b").write_text("b\n", encoding="utf-8")
    (repo / "link").symlink_to("target-a")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    base_tree = git(repo, "rev-parse", "HEAD^{tree}")

    (repo / "edit.txt").write_text("after\n", encoding="utf-8")
    (repo / "delete.txt").unlink()
    git(repo, "mv", "rename-old.txt", "rename-new.txt")
    (repo / "added.txt").write_text("added\n", encoding="utf-8")
    (repo / "script.sh").write_text("#!/bin/sh\necho new\n", encoding="utf-8")
    (repo / "script.sh").chmod(0o755)
    (repo / "binary.bin").write_bytes(b"\x00\x01new\xfe\xff")
    (repo / "link").unlink()
    (repo / "link").symlink_to("target-b")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    patch = git(
        repo,
        "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", base, candidate,
        text=False,
    )
    inventory = changed_inventory(repo, base, candidate)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
    return {"repo": repo, "bare": bare, "base": base, "base_tree": base_tree, "candidate": candidate, "tree": tree,
            "patch": patch, "inventory": inventory}


def build_transport(fx, *, expected_tree: str | None = None):
    patch = fx["patch"]
    cut = max(1, len(patch) // 2)
    raw_chunks = [patch[:cut], patch[cut:]] if cut < len(patch) else [patch]
    chunks = [
        {"index": i, "blob_sha": pm.git_blob_sha(raw), "byte_length": len(raw), "sha256": pm.sha256_bytes(raw)}
        for i, raw in enumerate(raw_chunks)
    ]
    request_id = "11111111-1111-4111-8111-111111111111"
    tree = expected_tree or fx["tree"]
    value = {
        "schema": pm.MANIFEST_SCHEMA,
        "request_id": request_id,
        "repository": {"full_name": "marcogallotta/ai-tools", "id": 1304888921},
        "task_gid": "1217471395822358",
        "pr_number": 123,
        "branch": "agent/materializer-test",
        "expected_old_head": fx["base"],
        "expected_final_tree": tree,
        "changed_paths": list(fx["inventory"]),
        "patch": {"byte_length": len(patch), "sha256": pm.sha256_bytes(patch)},
        "chunks": chunks,
        "limits": {
            "max_chunk_bytes": pm.MAX_CHUNK_BYTES,
            "max_chunks": pm.MAX_CHUNKS,
            "max_patch_bytes": pm.MAX_PATCH_BYTES,
            "max_changed_paths": pm.MAX_CHANGED_PATHS,
        },
    }
    raw_manifest = pm.canonical_json(value)
    request = pm.RequestIdentity(
        request_id=request_id,
        manifest_blob=pm.git_blob_sha(raw_manifest),
        manifest_sha256=pm.sha256_bytes(raw_manifest),
        repository_id=1304888921,
        task_gid="1217471395822358",
        pr_number=123,
        branch="agent/materializer-test",
        expected_old_head=fx["base"],
        expected_final_tree=tree,
    )
    manifest = pm.parse_manifest(raw_manifest, request, "marcogallotta/ai-tools")
    admission = pm.Admission("marcogallotta/ai-tools", 1304888921, request, manifest, 77, "marcogallotta")
    blobs = {pm.git_blob_sha(raw): raw for raw in raw_chunks}
    return admission, raw_manifest, blobs, value


def request_body(request) -> str:
    return (
        f"<!-- {pm.REQUEST_MARKER} request={request.request_id} manifest={request.manifest_blob} "
        f"manifest_sha256={request.manifest_sha256} repository_id={request.repository_id} task={request.task_gid} "
        f"pr={request.pr_number} branch={request.branch} head={request.expected_old_head} tree={request.expected_final_tree} -->"
    )


def test_reconstructs_exact_tree_for_binary_modes_symlink_add_delete_rename(candidate_repo):
    admission, _, _, _ = build_transport(candidate_repo)
    plan = pm.reconstruct_tree(admission, candidate_repo["patch"], remote_url=str(candidate_repo["bare"]))
    assert plan.tree_sha == candidate_repo["tree"]
    assert plan.base_tree_sha == candidate_repo["base_tree"]
    assert tuple(entry["path"] for entry in plan.entries) == candidate_repo["inventory"]
    by_path = {entry["path"]: entry for entry in plan.entries}
    assert by_path["delete.txt"]["sha"] is None
    assert by_path["script.sh"]["mode"] == "100755"
    assert by_path["link"]["mode"] == "120000"
    assert {"rename-old.txt", "rename-new.txt", "binary.bin", "added.txt"} <= set(by_path)


def test_corruption_reorder_duplicate_and_tree_mismatch_fail_closed(candidate_repo):
    admission, _, blobs, value = build_transport(candidate_repo)
    first = admission.manifest.chunks[0]
    corrupt = dict(blobs)
    corrupt[first.blob_sha] = corrupt[first.blob_sha] + b"x"
    with pytest.raises(pm.MaterializerError, match="byte length mismatch"):
        pm.assemble_patch(admission.manifest, corrupt.__getitem__)

    reordered = json.loads(json.dumps(value))
    reordered["chunks"][0]["index"] = 1
    raw = pm.canonical_json(reordered)
    req = admission.request.__class__(**{**admission.request.__dict__, "manifest_blob": pm.git_blob_sha(raw), "manifest_sha256": pm.sha256_bytes(raw)})
    with pytest.raises(pm.MaterializerError, match="contiguous and ordered"):
        pm.parse_manifest(raw, req, admission.repository)

    duplicated = json.loads(json.dumps(value))
    if len(duplicated["chunks"]) > 1:
        duplicated["chunks"][1]["blob_sha"] = duplicated["chunks"][0]["blob_sha"]
        raw = pm.canonical_json(duplicated)
        req = admission.request.__class__(**{**admission.request.__dict__, "manifest_blob": pm.git_blob_sha(raw), "manifest_sha256": pm.sha256_bytes(raw)})
        with pytest.raises(pm.MaterializerError, match="duplicate chunk"):
            pm.parse_manifest(raw, req, admission.repository)

    wrong_tree = "0" * 40
    wrong, _, _, _ = build_transport(candidate_repo, expected_tree=wrong_tree)
    with pytest.raises(pm.MaterializerError, match="precommitted expected tree"):
        pm.reconstruct_tree(wrong, candidate_repo["patch"], remote_url=str(candidate_repo["bare"]))


def test_manifest_ceiling_is_fail_closed(candidate_repo):
    admission, _, _, value = build_transport(candidate_repo)
    oversized = json.loads(json.dumps(value))
    oversized["chunks"][0]["byte_length"] = pm.MAX_CHUNK_BYTES + 1
    oversized["patch"]["byte_length"] = sum(c["byte_length"] for c in oversized["chunks"])
    raw = pm.canonical_json(oversized)
    req = admission.request.__class__(**{**admission.request.__dict__, "manifest_blob": pm.git_blob_sha(raw), "manifest_sha256": pm.sha256_bytes(raw)})
    with pytest.raises(pm.MaterializerError, match="chunk.byte_length"):
        pm.parse_manifest(raw, req, admission.repository)


class FakeAdmissionGitHub:
    def __init__(self, admission, manifest_raw, *, permission="write", draft=True, fork=False, head=None, blocker=True):
        self.admission = admission
        self.manifest_raw = manifest_raw
        self.permission = permission
        self.draft = draft
        self.fork = fork
        self.head = head or admission.request.expected_old_head
        self.blocker = blocker

    def get_repository(self, repository):
        return {"id": 1304888921, "private": False, "default_branch": "main"}

    def collaborator_permission(self, repository, login):
        return self.permission

    def get_pr(self, repository, number):
        repo = {"full_name": "someone/fork" if self.fork else repository, "id": 999 if self.fork else 1304888921}
        body = f"{pm.BLOCKER_HEADING}\nState: LOCAL IMPLEMENTATION COMPLETION REQUIRED\n<!-- dish-owning-task:v1 task=1217471395822358 -->" if self.blocker else "no blocker\n<!-- dish-owning-task:v1 task=1217471395822358 -->"
        return {"state": "open", "draft": self.draft, "base": {"ref": "main"}, "head": {"ref": self.admission.request.branch, "sha": self.head, "repo": repo}, "body": body}

    def get_ref(self, repository, branch):
        return {"object": {"sha": self.head}}

    def list_issue_comments(self, repository, number):
        return [{"id": 77, "body": request_body(self.admission.request)}]

    def get_blob_bytes(self, repository, sha):
        assert sha == self.admission.request.manifest_blob
        return self.manifest_raw


def admission_event(admission):
    return {
        "repository": {"id": 1304888921, "full_name": "marcogallotta/ai-tools"},
        "issue": {"number": 123, "pull_request": {"url": "x"}},
        "comment": {"id": 77, "body": request_body(admission.request), "user": {"login": "marcogallotta"}},
    }


@pytest.mark.parametrize("kwargs,match", [
    ({"permission": "read"}, "does not have repository write"),
    ({"draft": False}, "open draft PR"),
    ({"fork": True}, "Fork PRs|fork PRs"),
    ({"head": "f" * 40}, "does not match the exact live PR head"),
    ({"blocker": False}, "canonical LOCAL IMPLEMENTATION COMPLETION"),
])
def test_admission_rejects_untrusted_or_stale_request(candidate_repo, kwargs, match):
    admission, manifest_raw, _, _ = build_transport(candidate_repo)
    github = FakeAdmissionGitHub(admission, manifest_raw, **kwargs)
    with pytest.raises(pm.MaterializerError, match=match):
        pm.admit_event(admission_event(admission), github)


def test_admission_accepts_exact_open_same_repo_draft_blocker(candidate_repo):
    admission, manifest_raw, _, _ = build_transport(candidate_repo)
    result = pm.admit_event(admission_event(admission), FakeAdmissionGitHub(admission, manifest_raw))
    assert result.request == admission.request
    assert result.manifest.expected_final_tree == candidate_repo["tree"]


def test_duplicate_uuid_with_conflicting_identity_is_rejected(candidate_repo):
    admission, manifest_raw, _, _ = build_transport(candidate_repo)
    github = FakeAdmissionGitHub(admission, manifest_raw)
    conflict = admission.request.__class__(**{**admission.request.__dict__, "expected_final_tree": "e" * 40})
    github.list_issue_comments = lambda repo, number: [
        {"id": 77, "body": request_body(admission.request)},
        {"id": 76, "body": request_body(conflict)},
    ]
    with pytest.raises(pm.MaterializerError, match="UUID was already used"):
        pm.admit_event(admission_event(admission), github)


class FakeMaterializeGitHub:
    def __init__(self, fx, blobs):
        self.fx = fx
        self.blobs = blobs
        self.created_entries = None
        self.candidate = "c" * 40

    def get_blob_bytes(self, repository, sha):
        return self.blobs[sha]

    def get_commit(self, repository, sha):
        if sha == self.fx["base"]:
            return {"tree": {"sha": self.fx["base_tree"]}, "parents": []}
        assert sha == self.candidate
        return {"tree": {"sha": self.fx["tree"]}, "parents": [{"sha": self.fx["base"]}]}

    def create_blob(self, repository, raw):
        return pm.git_blob_sha(raw)

    def create_tree(self, repository, base_tree, entries):
        assert base_tree == self.fx["base_tree"]
        self.created_entries = entries
        return self.fx["tree"]

    def create_commit(self, repository, message, tree, parent):
        assert tree == self.fx["tree"] and parent == self.fx["base"]
        assert "Asana-Task: 1217471395822358" in message
        return self.candidate


def test_materialize_creates_exact_one_parent_candidate_without_attaching_ref(candidate_repo):
    admission, _, blobs, _ = build_transport(candidate_repo)
    github = FakeMaterializeGitHub(candidate_repo, blobs)
    result = pm.materialize(admission, github, remote_url=str(candidate_repo["bare"]))
    assert result.parent == candidate_repo["base"]
    assert result.tree == candidate_repo["tree"]
    assert result.candidate_commit == "c" * 40
    assert github.created_entries is not None


def test_helper_contains_no_source_ref_merge_ready_or_asana_write_primitive():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert 'request("PATCH"' not in source
    assert 'request("PUT"' not in source
    assert "/git/refs" not in source
    assert "/merges" not in source
    assert "ready_for_review" not in source
    assert "app.asana.com" not in source
    assert "api.asana.com" not in source
    assert "import asana" not in source.lower()
