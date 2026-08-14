from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("pr_lifecycle", SCRIPTS / "pr_lifecycle.py")
assert SPEC and SPEC.loader
pr_lifecycle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pr_lifecycle
SPEC.loader.exec_module(pr_lifecycle)

HEAD = "a" * 40
NEW_HEAD = "b" * 40
NOW = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)


def pr(*, head=HEAD, draft=False, state="open", merged=False, body="Owning task: 1217443403986570"):
    return {
        "number": 31,
        "html_url": "https://github.com/marcogallotta/ai-tools/pull/31",
        "title": "Lifecycle test",
        "state": state,
        "draft": draft,
        "merged": merged,
        "merged_at": NOW.isoformat() if merged else None,
        "body": body,
        "head": {"sha": head, "ref": "agent/test"},
        "base": {"ref": "main", "sha": "c" * 40},
        "mergeable": True,
        "mergeable_state": "clean",
    }


def review(*, head=HEAD, verdict="MERGE", body_tail="TESTS TO RUN: NONE.", review_id=10):
    return {
        "id": review_id,
        "state": "COMMENTED",
        "commit_id": head,
        "submitted_at": NOW.isoformat(),
        "body": f"VERDICT: {verdict}\n{body_tail}\nReviewed head: {head}",
    }


def status(*, head=HEAD, state="success", run_id=700):
    return {
        "sha": head,
        "statuses": [
            {
                "context": "Dish / exact-head certification",
                "state": state,
                "updated_at": "2026-08-13T08:10:00Z",
                "target_url": f"https://github.com/marcogallotta/ai-tools/actions/runs/{run_id}",
            }
        ],
    }


def runs(*, head=HEAD, run_id=700, status_value="completed", conclusion="success"):
    return {
        "workflow_runs": [
            {
                "id": run_id,
                "run_attempt": 1,
                "path": ".github/workflows/ci.yml",
                "event": "pull_request_review",
                "pull_requests": [{"number": 31}],
                "status": status_value,
                "conclusion": conclusion,
                "run_started_at": "2026-08-13T08:00:00Z",
            }
        ]
    }


class FakeGitHub:
    repository = "marcogallotta/ai-tools"

    def __init__(self, candidate=None):
        self.pr = deepcopy(candidate or pr())
        self.comments = []
        self.reviews = []
        self.combined_status = status(head=self.pr["head"]["sha"])
        self.workflow_runs = runs(head=self.pr["head"]["sha"])
        self.events = []
        self.merge_response = {"merged": True, "sha": "d" * 40, "message": "Pull Request successfully merged"}
        self.merge_mutates = True

    def list_prs(self, *, include_closed=False):
        if self.pr["state"] != "open" and not include_closed:
            return []
        return [deepcopy(self.pr)]

    def get_pr(self, number):
        assert number == self.pr["number"]
        return deepcopy(self.pr)

    def get_comments(self, number):
        return deepcopy(self.comments)

    def get_reviews(self, number):
        return deepcopy(self.reviews)

    def get_combined_status(self, sha):
        return deepcopy(self.combined_status)

    def get_workflow_runs(self):
        return deepcopy(self.workflow_runs)

    def add_comment(self, number, body):
        self.events.append(("comment", body))
        item = {
            "id": len(self.comments) + 1,
            "body": body,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
        self.comments.append(item)
        return deepcopy(item)

    def merge(self, number, *, expected_head, method):
        self.events.append(("merge", expected_head, method))
        assert expected_head == self.pr["head"]["sha"]
        if self.merge_mutates and self.merge_response.get("merged") is True:
            self.pr["merged"] = True
            self.pr["merged_at"] = NOW.isoformat()
            self.pr["state"] = "closed"
        return deepcopy(self.merge_response)


def engine(gh, *, now=NOW, authority=False, capable=True):
    return pr_lifecycle.LifecycleEngine(
        gh,
        integration_authority=authority,
        integration_capable=capable,
        now=lambda: now,
    )


def lease_comment(*, head=HEAD, phase="review", lease="11111111-1111-1111-1111-111111111111", when=NOW):
    return {
        "id": 1,
        "body": f"<!-- dish-agent-lease:v1 phase={phase} head={head} lease={lease} owner=pr-lifecycle -->",
        "created_at": when.isoformat(),
        "updated_at": when.isoformat(),
    }


def test_head_movement_invalidates_exact_head_lease():
    gh = FakeGitHub(pr(head=NEW_HEAD))
    gh.comments = [lease_comment(head=HEAD)]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_READY
    assert state.active_leases == []


def test_expired_lease_does_not_deadlock_review_queue():
    gh = FakeGitHub()
    gh.comments = [lease_comment(when=NOW - timedelta(minutes=61))]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_READY
    assert state.active_leases == []


def test_restart_reconstructs_same_state_without_local_persistence():
    gh = FakeGitHub()
    gh.comments = [lease_comment()]
    first = engine(gh).inspect(gh.pr).json()
    second = engine(gh).inspect(gh.pr).json()
    assert second == first
    assert first["state"] == pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS.value


def test_formal_exact_head_review_supersedes_review_lease():
    gh = FakeGitHub()
    gh.comments = [lease_comment()]
    gh.reviews = [review(verdict="BLOCK")]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.CHANGES_REQUESTED
    assert not [lease for lease in state.active_leases if lease["phase"] == "review"]


class FakeWorkspace:
    def __init__(self):
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        key = pr_lifecycle.WorkspaceAgentDispatcher.idempotency_key(
            kwargs["repository"], kwargs["pr_number"], kwargs["head"], kwargs["review_class"]
        )
        return pr_lifecycle.WorkspaceDispatchResult(key, "https://chatgpt.com/c/1", "apirun_1")


def test_duplicate_poll_does_not_duplicate_chatgpt_dispatch():
    gh = FakeGitHub()
    lifecycle = engine(gh)
    workspace = FakeWorkspace()
    first = lifecycle.dispatch(workspace=workspace, local_reviewer=None)
    assert len(workspace.calls) == 1
    assert first[0].state == pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS
    second = lifecycle.dispatch(workspace=workspace, local_reviewer=None)
    assert len(workspace.calls) == 1
    assert second[0].state == pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS


def test_semantic_new_head_invalidates_prior_review():
    gh = FakeGitHub(pr(head=NEW_HEAD))
    gh.reviews = [review(head=HEAD, verdict="MERGE")]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_READY
    assert state.review_verdict is None


def test_draft_pr_is_excluded_from_ordinary_review_dispatch():
    gh = FakeGitHub(pr(draft=True))
    workspace = FakeWorkspace()
    values = engine(gh).dispatch(workspace=workspace, local_reviewer=None)
    assert values[0].state == pr_lifecycle.LifecycleState.AUTHORING
    assert workspace.calls == []


def test_merge_with_local_cert_updates_pr_before_human_notification():
    gh = FakeGitHub()
    gh.reviews = [review(body_tail="TESTS TO RUN: dish/scripts/dish-pg-native-certification --candidate aaaaa")]
    events = gh.events

    def notify(message):
        events.append(("notify", message))

    lifecycle = engine(gh)
    initial = lifecycle.inspect(gh.pr)
    assert initial.state == pr_lifecycle.LifecycleState.LOCAL_CERTIFICATION_REQUIRED
    assert initial.human_action is None
    result = lifecycle.dispatch_one(initial, workspace=None, local_reviewer=None, notify=notify)
    assert result.state == pr_lifecycle.LifecycleState.LOCAL_CERTIFICATION_REQUIRED
    handoff_index = next(i for i, event in enumerate(events) if event[0] == "comment" and "dish-local-handoff:v1" in event[1])
    notify_index = next(i for i, event in enumerate(events) if event[0] == "notify")
    assert handoff_index < notify_index
    assert result.local_work[0]["handoff_present"] is True


def test_merge_with_green_gates_and_explicit_authority_merges_exact_head_then_reads_back():
    gh = FakeGitHub()
    gh.reviews = [review()]
    lifecycle = engine(gh, authority=True)
    initial = lifecycle.inspect(gh.pr)
    assert initial.state == pr_lifecycle.LifecycleState.INTEGRATION_READY
    result = lifecycle.dispatch_one(initial, workspace=None, local_reviewer=None)
    assert result.state == pr_lifecycle.LifecycleState.MERGED
    merge_event = next(event for event in gh.events if event[0] == "merge")
    assert merge_event[1] == HEAD


def test_no_false_merged_state_from_merge_response_without_authoritative_readback():
    gh = FakeGitHub()
    gh.reviews = [review()]
    gh.merge_mutates = False
    lifecycle = engine(gh, authority=True)
    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert result.state == pr_lifecycle.LifecycleState.INTEGRATION_READY
    assert "readback" in result.residual_reason


def test_agent_prose_or_stale_review_never_advances_to_merged():
    gh = FakeGitHub(pr(head=NEW_HEAD, body="An agent said VERDICT: MERGE in chat"))
    gh.reviews = [review(head=HEAD, verdict="MERGE")]
    assert engine(gh, authority=True).inspect(gh.pr).state == pr_lifecycle.LifecycleState.REVIEW_READY



def test_green_gates_without_integration_authority_exposes_exact_residual_boundary():
    gh = FakeGitHub()
    gh.reviews = [review()]
    state = engine(gh, authority=False).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.INTEGRATION_READY
    assert "authority" in state.residual_reason.lower()


def test_local_implementation_completion_requirement_is_distinct_state():
    gh = FakeGitHub()
    gh.reviews = [
        review(body_tail="LOCAL IMPLEMENTATION COMPLETION REQUIRED: run local generator\nTESTS TO RUN: NONE.")
    ]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED
    assert state.local_work[0]["kind"] == "implementation"


def test_completed_local_certification_allows_gate_evaluation():
    gh = FakeGitHub()
    command = "dish/scripts/dish-pg-native-certification --candidate aaaaa"
    gh.reviews = [review(body_tail=f"TESTS TO RUN: {command}")]
    gh.comments = [
        {
            "id": 9,
            "body": f"<!-- dish-local-completion:v1 kind=certification head={HEAD} result=pass -->",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    ]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.INTEGRATION_READY


class RecordingHTTP:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url, dict(headers or {}), deepcopy(body)))
        return 202, {}, {"conversation_url": "https://chatgpt.com/c/123", "agent_trigger_run_id": "apirun_123"}


def test_workspace_agent_dispatch_uses_exact_identity_and_idempotency_header():
    http = RecordingHTTP()
    dispatcher = pr_lifecycle.WorkspaceAgentDispatcher(
        access_token="secret",
        review_trigger_id="agtch_review",
        http=http,
    )
    first = dispatcher.dispatch(
        repository="marcogallotta/ai-tools",
        pr_number=31,
        pr_url="https://github.com/marcogallotta/ai-tools/pull/31",
        head=HEAD,
        review_class="substantive",
        task_ids=["1217443403986570"],
    )
    second_key = dispatcher.idempotency_key("marcogallotta/ai-tools", 31, HEAD, "substantive")
    assert first.idempotency_key == second_key
    method, url, headers, body = http.calls[0]
    assert method == "POST"
    assert url.endswith("/workspace_agents/agtch_review/trigger")
    assert headers["Idempotency-Key"] == second_key
    assert headers["OpenAI-Beta"] == "workspace_agent_runs=v1"
    assert HEAD in body["input"]
    assert "1217443403986570" in body["input"]
    assert "dish/docs/agents/review.md" in body["input"]


def test_domain_dispatch_uses_the_one_ordinary_reviewer_not_a_second_specialist():
    dispatcher = pr_lifecycle.WorkspaceAgentDispatcher(
        access_token="secret",
        review_trigger_id="agtch_review",
        http=RecordingHTTP(),
    )
    # A domain label never selects a different Workspace Agent trigger: the same
    # ordinary reviewer handles both substantive and domain-deep review classes.
    assert dispatcher.trigger_id_for("domain:postgresql") == "agtch_review"
    assert dispatcher.trigger_id_for("specialist:postgresql") == "agtch_review"
    assert dispatcher.trigger_id_for("substantive") == "agtch_review"


def test_domain_dispatch_prompt_instructs_deeper_in_workflow_scrutiny():
    http = RecordingHTTP()
    dispatcher = pr_lifecycle.WorkspaceAgentDispatcher(
        access_token="secret",
        review_trigger_id="agtch_review",
        http=http,
    )
    dispatcher.dispatch(
        repository="marcogallotta/ai-tools",
        pr_number=43,
        pr_url="https://github.com/marcogallotta/ai-tools/pull/43",
        head=HEAD,
        review_class="domain:postgresql",
        task_ids=["1217463570624074"],
    )
    _, url, _, body = http.calls[0]
    assert url.endswith("/workspace_agents/agtch_review/trigger")
    assert "postgresql" in body["input"]
    assert "deepen your own" in body["input"]
    assert "separate specialist reviewer" in body["input"]


def test_legacy_specialist_route_normalizes_to_domain_depth_hint():
    candidate = pr(body="Owning task: 1217443403986570\nREVIEW CLASS: specialist:postgresql")
    gh = FakeGitHub(candidate)
    state = engine(gh).inspect(gh.pr)
    assert state.review_class == "domain:postgresql"


def test_explicit_domain_route_stays_in_one_review_workflow():
    candidate = pr(body="Owning task: 1217443403986570\nREVIEW CLASS: domain:postgresql")
    gh = FakeGitHub(candidate)
    state = engine(gh).inspect(gh.pr)
    assert state.review_class == "domain:postgresql"


def test_explicit_focused_route_is_eligible_for_bounded_local_adapter_only():
    candidate = pr(body="Owning task: 1217443403986570\nREVIEW CLASS: focused")
    gh = FakeGitHub(candidate)
    state = engine(gh).inspect(gh.pr)
    assert state.review_class == "focused"


def test_old_block_focused_recheck_routes_new_head_as_focused():
    gh = FakeGitHub(pr(head=NEW_HEAD))
    gh.reviews = [
        review(
            head=HEAD,
            verdict="BLOCK",
            body_tail="TESTS TO RUN: NONE.\nFOCUSED RECHECK",
        )
    ]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_READY
    assert state.review_class == "focused"


def test_old_block_domain_deep_recheck_stays_in_one_review_workflow():
    gh = FakeGitHub(pr(head=NEW_HEAD))
    gh.reviews = [
        review(
            head=HEAD,
            verdict="BLOCK",
            body_tail="TESTS TO RUN: NONE.\nDOMAIN DEEP RECHECK",
        )
    ]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_READY
    assert state.review_class == "domain:unspecified"


def test_restart_resumes_dispatcher_owned_integration_lease():
    gh = FakeGitHub()
    gh.reviews = [review()]
    gh.comments = [lease_comment(phase="integration")]
    lifecycle = engine(gh, authority=True)
    initial = lifecycle.inspect(gh.pr)
    assert initial.state == pr_lifecycle.LifecycleState.MERGING
    result = lifecycle.dispatch_one(initial, workspace=None, local_reviewer=None)
    assert result.state == pr_lifecycle.LifecycleState.MERGED
    assert next(event for event in gh.events if event[0] == "merge")[1] == HEAD


def test_foreign_integration_lease_is_not_taken_over_before_stale():
    gh = FakeGitHub()
    gh.reviews = [review()]
    foreign = lease_comment(phase="integration")
    foreign["body"] = foreign["body"].replace("owner=pr-lifecycle", "owner=another-integrator")
    gh.comments = [foreign]
    lifecycle = engine(gh, authority=True)
    initial = lifecycle.inspect(gh.pr)
    assert initial.state == pr_lifecycle.LifecycleState.MERGING
    result = lifecycle.dispatch_one(initial, workspace=None, local_reviewer=None)
    assert result.state == pr_lifecycle.LifecycleState.MERGING
    assert not any(event[0] == "merge" for event in gh.events)


def test_local_action_notice_is_idempotent_across_duplicate_polls():
    gh = FakeGitHub()
    command = "dish/scripts/dish-pg-native-certification --candidate aaaaa"
    gh.reviews = [review(body_tail=f"TESTS TO RUN: {command}")]
    notices = []
    lifecycle = engine(gh)
    first = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, notify=notices.append)
    second = lifecycle.dispatch_one(first, workspace=None, local_reviewer=None, notify=notices.append)
    assert len(notices) == 1
    handoff_index = next(i for i, event in enumerate(gh.events) if event[0] == "comment" and "dish-local-handoff:v1" in event[1])
    notice_index = next(i for i, event in enumerate(gh.events) if event[0] == "comment" and "dish-human-notice:v1" in event[1])
    assert handoff_index < notice_index
    assert sum("dish-human-notice:v1" in event[1] for event in gh.events if event[0] == "comment") == 1
    assert second.state == pr_lifecycle.LifecycleState.LOCAL_CERTIFICATION_REQUIRED


def test_merge_review_without_tests_to_run_remains_evaluating_gates():
    gh = FakeGitHub()
    gh.reviews = [review(body_tail="No local evidence line supplied.")]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert "TESTS TO RUN" in (state.residual_reason or "")


def test_review_dispatch_configuration_notice_is_idempotent():
    gh = FakeGitHub()
    notices = []
    lifecycle = engine(gh)
    first = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, notify=notices.append)
    second = lifecycle.dispatch_one(first, workspace=None, local_reviewer=None, notify=notices.append)
    assert len(notices) == 1
    assert sum("dish-human-notice:v1" in event[1] for event in gh.events if event[0] == "comment") == 1
