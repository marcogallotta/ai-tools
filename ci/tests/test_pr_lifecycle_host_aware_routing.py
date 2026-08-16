from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
import zipfile

import test_pr_lifecycle as base
import pr_implementation_provenance as provenance
import pr_mutation_broker as broker
from pr_lifecycle_helpers_base import _verified_route_result_host

p = base.pr_lifecycle


class RecordingFixRouter:
    def __init__(self, *, chatgpt=True, local=True):
        self.commands = {
            "CHATGPT_IMPLEMENTATION": "chatgpt" if chatgpt else None,
            "LOCAL_IMPLEMENTATION": "local" if local else None,
        }
        self.calls = []

    @property
    def command(self):
        return next((v for v in self.commands.values() if v), None)

    def command_for(self, host):
        return self.commands.get(host)

    def dispatch(self, context, *, host):
        self.calls.append((host, context))


class RecordingReview:
    command = "configured"

    def __init__(self):
        self.calls = []

    def dispatch(self, context):
        self.calls.append(context)


class RecordingWorkspace:
    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        return p.WorkspaceDispatchResult("key", None, None)


class FakeAsana:
    def __init__(self, stories):
        self.stories = deepcopy(stories)

    def get_stories(self, gid):
        assert gid == "1217443403986570"
        return deepcopy(self.stories)


def _prelaunch_story(*, base_sha="c" * 40):
    task = "1217443403986570"
    source = provenance._expected_source(
        repository="marcogallotta/ai-tools",
        task_gid=task,
        branch="agent/test",
        base_ref="refs/heads/main",
        base_sha=base_sha,
    )
    handoff = provenance._handoff_identity(task_gid=task, timestamp=base.NOW, source=source)
    text = "\n".join((
        f"<!-- dish-implementation-handoff:v1 handoff={handoff} task={task} role=Implementation at={base.NOW.isoformat()} -->",
        "AUTHORIZED IMPLEMENTATION HANDOFF",
        f"Task: {task}",
        "Target role: Implementation",
        f"Handoff time: {base.NOW.isoformat()}",
        f"Source: {source}",
        "Branch: agent/test",
        f"Base: {base_sha}",
        "PR: not yet known",
        "Head: not yet known",
        "A missing PR/branch delta does not mean this handoff was not sent.",
        "— Dish Agent: Development Workflow | repository control plane",
    ))
    return handoff, {"gid": "story-1", "text": text, "created_at": base.NOW.isoformat()}


def _install_prelaunch_cache(gh, *, handoff, base_sha="c" * 40):
    task, run_id, attempt, artifact_id = "1217443403986570", 701, 1, 702
    launcher = f"asana-handoff:{handoff}"
    proof = {
        "schema": "dish-implementation-prelaunch-proof-v2",
        "repository": gh.repository,
        "pr_number": 31,
        "task_gid": task,
        "branch": "agent/test",
        "head": base.HEAD,
        "base_ref": "refs/heads/main",
        "base_sha": base_sha,
        "host": "chatgpt",
        "launcher": launcher,
        "handoff_id": handoff,
        "run_id": run_id,
        "run_attempt": attempt,
    }
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("implementation-provenance.json", json.dumps(proof, sort_keys=True))
    archive = archive_buffer.getvalue()
    digest = f"sha256:{hashlib.sha256(archive).hexdigest()}"
    gh.workflow_attempts[(run_id, attempt)] = {
        "id": run_id,
        "run_attempt": attempt,
        "event": "repository_dispatch",
        "conclusion": "success",
        "path": ".github/workflows/pr-implementation-provenance.yml",
        "run_started_at": (base.NOW + timedelta(minutes=1)).isoformat(),
    }
    gh.run_artifacts[run_id] = [{"id": artifact_id, "digest": digest, "expired": False}]
    gh.artifacts[artifact_id] = archive
    gh.comments = [{
        "id": 1,
        "body": (
            f"<!-- dish-implementation-host-witness:v1 head={base.HEAD} host=chatgpt "
            f"source=orchestration launcher={launcher} task={task} handoff={handoff} "
            f"base_ref=refs/heads/main base_sha={base_sha} run={run_id} attempt={attempt} "
            f"artifact={artifact_id} digest={digest} -->"
        ),
        "created_at": base.NOW.isoformat(),
        "updated_at": base.NOW.isoformat(),
    }]


def test_block_fix_defaults_to_chatgpt_implementation():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK", body_tail="TESTS TO RUN: NONE.")]
    fixer = RecordingFixRouter()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, implementation_fixer=fixer
    )
    assert [host for host, _ in fixer.calls] == ["CHATGPT_IMPLEMENTATION"]


def test_exact_class_b_block_can_select_local_implementation():
    gh = base.FakeGitHub()
    gh.reviews = [
        base.review(
            verdict="BLOCK",
            body_tail=(
                "LOCAL IMPLEMENTATION COMPLETION REQUIRED: "
                "IMPLEMENTATION / PUBLICATION — hosted publication cannot write governed path; "
                "fallbacks exhausted: connector update, Git data API\n"
                "TESTS TO RUN: NONE."
            ),
        )
    ]
    fixer = RecordingFixRouter()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, implementation_fixer=fixer
    )
    assert [host for host, _ in fixer.calls] == ["LOCAL_IMPLEMENTATION"]


def test_remote_consumer_unavailable_never_falls_back_to_configured_local_consumer():
    gh = base.FakeGitHub()
    gh.reviews = [base.review(verdict="BLOCK", body_tail="TESTS TO RUN: NONE.")]
    fixer = RecordingFixRouter(chatgpt=False, local=True)
    lifecycle = base.engine(gh)
    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, implementation_fixer=fixer
    )
    assert fixer.calls == []
    assert result.state == p.LifecycleState.CHANGES_REQUESTED
    assert "CHATGPT_IMPLEMENTATION" in (result.residual_reason or "")


def test_bounded_local_review_requires_positive_chatgpt_implementation_witness():
    candidate = base.pr(body="Owning task: 1217443403986570\nREVIEW CLASS: focused")
    gh = base.FakeGitHub(candidate)
    local = RecordingReview()
    workspace = RecordingWorkspace()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=workspace, local_reviewer=local)
    assert local.calls == []
    assert len(workspace.calls) == 1


def test_self_asserted_prepr_chatgpt_witness_routes_to_chatgpt_review():
    candidate = base.pr(
        body=(
            "Owning task: 1217443403986570\nREVIEW CLASS: focused\n"
            f"<!-- dish-implementation-host-witness:v1 head={base.HEAD} host=chatgpt "
            "source=orchestration launcher=dispatch-123 -->"
        )
    )
    gh = base.FakeGitHub(candidate)
    local = RecordingReview()
    workspace = RecordingWorkspace()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=workspace, local_reviewer=local)
    assert local.calls == []
    assert len(workspace.calls) == 1


def test_successful_prelaunch_cache_without_independent_lineage_routes_to_chatgpt_review(monkeypatch):
    candidate = base.pr(body="Owning task: 1217443403986570\nREVIEW CLASS: focused")
    gh = base.FakeGitHub(candidate)
    handoff, _ = _prelaunch_story()
    _install_prelaunch_cache(gh, handoff=handoff)
    monkeypatch.setattr(provenance, "_asana_from_environment", lambda: FakeAsana([]))
    local = RecordingReview()
    workspace = RecordingWorkspace()
    p.LifecycleEngine(gh).dispatch_one(
        p.LifecycleEngine(gh).inspect(gh.pr), workspace=workspace, local_reviewer=local
    )
    assert local.calls == []
    assert len(workspace.calls) == 1


def test_independently_backed_prelaunch_chatgpt_lineage_allows_bounded_local_review(monkeypatch):
    candidate = base.pr(body="Owning task: 1217443403986570\nREVIEW CLASS: focused")
    gh = base.FakeGitHub(candidate)
    handoff, story = _prelaunch_story()
    _install_prelaunch_cache(gh, handoff=handoff)
    monkeypatch.setattr(provenance, "_asana_from_environment", lambda: FakeAsana([story]))
    local = RecordingReview()
    workspace = RecordingWorkspace()
    p.LifecycleEngine(gh).dispatch_one(
        p.LifecycleEngine(gh).inspect(gh.pr), workspace=workspace, local_reviewer=local
    )
    assert len(local.calls) == 1
    assert workspace.calls == []


def test_stale_prelaunch_handoff_routes_to_chatgpt_review(monkeypatch):
    candidate = base.pr(body="Owning task: 1217443403986570\nREVIEW CLASS: focused")
    gh = base.FakeGitHub(candidate)
    handoff, story = _prelaunch_story()
    _install_prelaunch_cache(gh, handoff=handoff)
    stale_notice = {
        "gid": "story-2",
        "text": f"<!-- dish-stale-handoff:v1 handoff={handoff} -->",
        "created_at": (base.NOW + timedelta(minutes=2)).isoformat(),
    }
    monkeypatch.setattr(provenance, "_asana_from_environment", lambda: FakeAsana([story, stale_notice]))
    local = RecordingReview()
    workspace = RecordingWorkspace()
    p.LifecycleEngine(gh).dispatch_one(
        p.LifecycleEngine(gh).inspect(gh.pr), workspace=workspace, local_reviewer=local
    )
    assert local.calls == []
    assert len(workspace.calls) == 1


def test_tampered_or_ambiguous_prelaunch_lineage_routes_to_chatgpt_review(monkeypatch):
    candidate = base.pr(body="Owning task: 1217443403986570\nREVIEW CLASS: focused")
    gh = base.FakeGitHub(candidate)
    handoff, story = _prelaunch_story()
    _install_prelaunch_cache(gh, handoff=handoff)
    tampered = deepcopy(story)
    tampered["text"] = tampered["text"].replace("host=chatgpt", "host=local")

    for stories in ([tampered], [story, deepcopy(story)]):
        monkeypatch.setattr(provenance, "_asana_from_environment", lambda stories=stories: FakeAsana(stories))
        local = RecordingReview()
        workspace = RecordingWorkspace()
        p.LifecycleEngine(gh).dispatch_one(
            p.LifecycleEngine(gh).inspect(gh.pr), workspace=workspace, local_reviewer=local
        )
        assert local.calls == []
        assert len(workspace.calls) == 1


def test_positive_local_implementation_witness_forces_chatgpt_review():
    candidate = base.pr(
        body=(
            "Owning task: 1217443403986570\nREVIEW CLASS: focused\n"
            f"<!-- dish-implementation-host-witness:v1 head={base.HEAD} host=local "
            "source=orchestration launcher=dispatch-456 -->"
        )
    )
    gh = base.FakeGitHub(candidate)
    local = RecordingReview()
    workspace = RecordingWorkspace()
    lifecycle = base.engine(gh)
    lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=workspace, local_reviewer=local)
    assert local.calls == []
    assert len(workspace.calls) == 1


def test_route_result_host_must_match_broker_accepted_consumer_and_terminal_head(monkeypatch):
    candidate = base.pr()
    grant = broker.GrantState(
        grant_id="grant-1", generation=1, action="fix", task_gid="1217443403986570",
        pr_number=31, branch="agent/test", starting_head="c" * 40, review_id=10,
        main_sha=None, route="fix-local", consumer_id="consumer", issued_at=base.NOW,
        stale_after=base.NOW, event_comment_id=99, closed=True,
        accepted_host="local", result_head=base.HEAD,
    )
    monkeypatch.setattr(broker, "current_verified_grant", lambda **_: grant)
    candidate["head"]["sha"] = base.HEAD
    gh = base.FakeGitHub(candidate)
    gh.get_repository_id = lambda: 1
    fields = {"start": "c" * 40, "head": base.HEAD, "route": "fix-local", "grant": "grant-1",
              "generation": "1", "broker_event": "99", "host": "chatgpt"}
    assert _verified_route_result_host(candidate, [], fields, current_head=base.HEAD, github=gh) is None
