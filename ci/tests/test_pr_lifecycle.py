from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

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


def test_projection_health_consumes_exact_full_regression_artifact(monkeypatch):
    first_evidence = {
        "schema": "dish-full-regression-v1",
        "run_id": "700",
        "run_attempt": 1,
        "main_sha": NEW_HEAD,
        "event": "schedule",
        "overall_result": "failed",
        "failures": [],
    }
    second_evidence = {**first_evidence, "run_attempt": 2, "overall_result": "passed"}
    def archive(value):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr(
                ".test-artifacts/full-regression/evidence.json",
                json.dumps(value),
            )
        return output.getvalue()
    archives = {701: archive(first_evidence), 702: archive(second_evidence)}
    class GitHub:
        artifact_reads = 0
        current_attempt = 1
        available_attempts = 1
        def full_regression_runs(self):
            return {"workflow_runs": [{
                "id": 700,
                "run_attempt": self.current_attempt,
                "event": "schedule",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": NEW_HEAD,
            }]}
        def get_run_artifacts(self, run_id):
            self.artifact_reads += 1
            assert run_id == 700
            return [
                {"id": value, "name": f"full-regression-{NEW_HEAD}", "expired": False}
                for value in range(701, 701 + self.available_attempts)
            ]
        def download_artifact(self, artifact_id):
            return archives[artifact_id]
    class Engine:
        github = GitHub()
    monkeypatch.setattr(pr_lifecycle.pr_lifecycle_controller, "_paths", lambda: None)
    monkeypatch.setattr(
        pr_lifecycle.pr_lifecycle_controller,
        "_snapshot",
        lambda paths: {"status": "ok"},
    )
    engine = Engine()
    _, result = pr_lifecycle._projection_health(engine)
    assert result["evidence"] == first_evidence
    assert result["evidence_artifact_id"] == 701
    _, replay = pr_lifecycle._projection_health(
        engine,
        previous_full_regression=result,
    )
    assert replay["evidence"] == first_evidence
    assert engine.github.artifact_reads == 1
    engine.github.current_attempt = 2
    engine.github.available_attempts = 2
    _, rerun = pr_lifecycle._projection_health(
        engine,
        previous_full_regression=result,
    )
    assert rerun["run_attempt"] == 2
    assert rerun["evidence"] == second_evidence
    assert rerun["evidence_artifact_id"] == 702
    _, rerun_replay = pr_lifecycle._projection_health(
        engine,
        previous_full_regression=rerun,
    )
    assert rerun_replay["evidence"] == second_evidence
    assert engine.github.artifact_reads == 2
    engine.github.current_attempt = 3
    _, stale = pr_lifecycle._projection_health(
        engine,
        previous_full_regression=rerun,
    )
    assert stale["status"] == "unavailable"
    assert "exact completed run attempt" in stale["error"]


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
        self.repository_id = 1304888921
        self.workflow_attempts = {}
        self.run_artifacts = {}
        self.artifacts = {}
        self.merge_response = {"merged": True, "sha": "d" * 40, "message": "Pull Request successfully merged"}
        self.merge_mutates = True
        self.refs = {"heads/main": "c" * 40}
        self.pr_files = []

    def list_prs(self, *, include_closed=False):
        if self.pr["state"] != "open" and not include_closed:
            return []
        return [deepcopy(self.pr)]

    def get_pr(self, number):
        assert number == self.pr["number"]
        return deepcopy(self.pr)

    def get_pr_files(self, number):
        assert number == self.pr["number"]
        return deepcopy(self.pr_files)

    def get_comments(self, number):
        return deepcopy(self.comments)

    def get_reviews(self, number):
        return deepcopy(self.reviews)

    def get_combined_status(self, sha):
        return deepcopy(self.combined_status)

    def get_workflow_runs(self):
        return deepcopy(self.workflow_runs)

    def get_repository_id(self):
        return self.repository_id

    def get_ref_sha(self, ref):
        return self.refs[ref]

    def get_workflow_run_attempt(self, run_id, run_attempt):
        return deepcopy(self.workflow_attempts[(run_id, run_attempt)])

    def get_run_artifacts(self, run_id):
        return deepcopy(self.run_artifacts.get(run_id, []))

    def download_artifact(self, artifact_id):
        return self.artifacts[artifact_id]

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
            self.pr["merge_commit_sha"] = self.merge_response.get("sha")
            self.pr["state"] = "closed"
        return deepcopy(self.merge_response)



class FakeLocalIntegration:
    command = "fake-local-integration"

    def __init__(self, github: FakeGitHub, *, outcome: str = "merge"):
        self.github = github
        self.outcome = outcome
        self.calls = []

    def dispatch(self, context, *, lock_fd=None):
        self.calls.append(deepcopy(context))
        self.github.events.append(("local-integration", context["pull_request"]["head"], self.outcome))
        if self.outcome == "merge":
            self.github.merge(
                context["pull_request"]["number"],
                expected_head=context["pull_request"]["head"],
                method=context.get("merge_method", "squash"),
            )
        elif self.outcome == "head-change":
            self.github.pr["head"]["sha"] = NEW_HEAD
        elif self.outcome == "no-readback":
            prior = self.github.merge_mutates
            self.github.merge_mutates = False
            try:
                self.github.merge(
                    context["pull_request"]["number"],
                    expected_head=context["pull_request"]["head"],
                    method="squash",
                )
            finally:
                self.github.merge_mutates = prior
        elif self.outcome == "return":
            return
        else:
            raise AssertionError(f"unknown fake local Integration outcome: {self.outcome}")

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


class PagedHistoryGitHub(FakeGitHub):
    """Open work plus a deliberately large terminal history."""

    def __init__(self):
        super().__init__()
        self.full_history_reads = 0
        self.closed_recovery_slots = []
        self.closed_history_size = 10_000
        self.closed_candidates = {}

    def list_prs(self, *, include_closed=False):
        if include_closed:
            self.full_history_reads += 1
            raise AssertionError("watch dispatch must not read all closed PRs")
        return [deepcopy(self.pr)]

    def closed_recovery_candidate(self, *, recovery_slot):
        self.closed_recovery_slots.append(recovery_slot)
        page = (recovery_slot % self.closed_history_size) + 1
        candidate = deepcopy(self.pr)
        candidate["number"] = 10_000 + page
        candidate["state"] = "closed"
        candidate["merged"] = False
        candidate["merged_at"] = None
        candidate["head"]["ref"] = "main"
        self.closed_candidates[candidate["number"]] = candidate
        return deepcopy(candidate)

    def get_pr(self, number):
        if number in self.closed_candidates:
            return deepcopy(self.closed_candidates[number])
        return super().get_pr(number)


def test_fresh_dispatch_processes_rotate_closed_recovery_with_large_history():
    gh = PagedHistoryGitHub()
    first = engine(gh, now=NOW).dispatch(workspace=None, local_reviewer=None)
    second = engine(gh, now=NOW + timedelta(seconds=180)).dispatch(workspace=None, local_reviewer=None)

    assert first[0].number == second[0].number == 31
    assert first[1].number != second[1].number
    assert gh.full_history_reads == 0
    assert gh.closed_recovery_slots == [
        int(NOW.timestamp() // 180),
        int((NOW + timedelta(seconds=180)).timestamp() // 180),
    ]


def test_json_status_render_includes_generation_time():
    gh = FakeGitHub()
    value = engine(gh).inspect(gh.pr)
    rendered = json.loads(pr_lifecycle._render_json([value], repository="marcogallotta/ai-tools"))

    assert rendered["generated_at"]
    assert rendered["pull_requests"][0]["number"] == 31


def test_http_timeout_is_bounded_and_reported(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 7.0
        raise TimeoutError("timed out")

    monkeypatch.setattr("pr_lifecycle_support.urlrequest.urlopen", timeout)
    client = pr_lifecycle.JSONHTTPClient(timeout=7.0)

    with pytest.raises(pr_lifecycle.LifecycleError, match=r"timed out after 7s"):
        client.request("GET", "https://example.invalid/test")


def test_projection_http_budget_counts_every_request_before_transport(monkeypatch):
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise OSError("fixture transport")

    monkeypatch.setattr("pr_lifecycle_support.urlrequest.urlopen", unavailable)
    budget = pr_lifecycle.ObservationBudget(max_requests=1, max_seconds=60)
    client = pr_lifecycle.JSONHTTPClient(timeout=7.0, budget=budget)
    with pytest.raises(OSError, match="fixture transport"):
        client.request("GET", "https://example.invalid/first")
    with pytest.raises(pr_lifecycle.ObservationBudgetError, match="request budget exhausted"):
        client.request("GET", "https://example.invalid/second")
    assert calls == [True]


def test_projection_http_timeout_is_fail_closed_budget_exhaustion(monkeypatch):
    monkeypatch.setattr(
        "pr_lifecycle_support.urlrequest.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    client = pr_lifecycle.JSONHTTPClient(
        timeout=10.0,
        budget=pr_lifecycle.ObservationBudget(max_requests=10, max_seconds=60),
    )
    with pytest.raises(pr_lifecycle.ObservationBudgetError, match="per-request budget exhausted"):
        client.request("GET", "https://example.invalid/test")


def test_projection_wall_budget_exhausts_before_transport(monkeypatch):
    budget = pr_lifecycle.ObservationBudget(max_requests=10, max_seconds=1)
    budget.started -= 2
    client = pr_lifecycle.JSONHTTPClient(timeout=10.0, budget=budget)
    with pytest.raises(pr_lifecycle.ObservationBudgetError, match="wall budget exhausted"):
        client.request("GET", "https://example.invalid/test")


def test_closed_recovery_reads_one_explicit_closed_page():
    class ClosedPageHTTP:
        def __init__(self):
            self.calls = []

        def request(self, method, url, *, headers=None, body=None):
            self.calls.append((method, url, dict(headers or {}), body))
            return 200, {}, []

    http = ClosedPageHTTP()
    github = pr_lifecycle.GitHubREST("marcogallotta/ai-tools", "token", http=http)

    assert github.list_closed_prs_page(page=4, per_page=1) == []
    assert "state=closed" in http.calls[0][1]
    assert "page=4" in http.calls[0][1]
    assert "per_page=1" in http.calls[0][1]


def test_closed_recovery_candidate_selects_a_page_from_github_pagination():
    class RecoveryHTTP:
        def __init__(self):
            self.calls = []

        def request(self, method, url, *, headers=None, body=None):
            self.calls.append(url)
            if pr_lifecycle.urlparse.parse_qs(pr_lifecycle.urlparse.urlparse(url).query)["page"] == ["1"]:
                return 200, {"Link": '<https://api.github.com/repos/marcogallotta/ai-tools/pulls?page=3>; rel="last"'}, [pr()]
            return 200, {}, [pr(head=NEW_HEAD)]

    http = RecoveryHTTP()
    github = pr_lifecycle.GitHubREST("marcogallotta/ai-tools", "token", http=http)

    candidate = github.closed_recovery_candidate(recovery_slot=1)

    assert candidate["head"]["sha"] == NEW_HEAD
    assert len(http.calls) == 2
    assert "page=2" in http.calls[1]


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


def test_merge_with_green_gates_and_explicit_authority_uses_local_launcher_then_reads_back(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = FakeGitHub()
    gh.reviews = [review()]
    lifecycle = engine(gh, authority=True)
    launcher = FakeLocalIntegration(gh, outcome="merge")
    lifecycle.local_integration_launcher = launcher
    initial = lifecycle.inspect(gh.pr)
    assert initial.state == pr_lifecycle.LifecycleState.INTEGRATION_READY
    result = lifecycle.dispatch_one(initial, workspace=None, local_reviewer=None)
    assert result.state == pr_lifecycle.LifecycleState.MERGED
    assert len(launcher.calls) == 1
    assert launcher.calls[0]["schema"] == "dish-pr-local-integration-v1"
    assert launcher.calls[0]["claim"]["head"] == HEAD
    assert next(event for event in gh.events if event[0] == "local-integration")[1] == HEAD
    assert next(event for event in gh.events if event[0] == "merge")[1] == HEAD


def test_no_false_merged_state_from_local_launcher_without_authoritative_readback(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = FakeGitHub()
    gh.reviews = [review()]
    lifecycle = engine(gh, authority=True)
    launcher = FakeLocalIntegration(gh, outcome="no-readback")
    lifecycle.local_integration_launcher = launcher
    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert result.state == pr_lifecycle.LifecycleState.INTEGRATION_READY
    assert "without authoritative MERGED readback" in result.residual_reason


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
    assert first.accepted is True and first.status_code == 202
    method, url, headers, body = http.calls[0]
    assert method == "POST"
    assert url.endswith("/workspace_agents/agtch_review/trigger")
    assert headers["Idempotency-Key"] == second_key
    assert headers["OpenAI-Beta"] == "workspace_agent_runs=v1"
    assert HEAD in body["input"]
    assert "1217443403986570" in body["input"]
    assert "dish/docs/agents/review.md" in body["input"]




class EmptyAcceptedHTTP(RecordingHTTP):
    def request(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url, dict(headers or {}), deepcopy(body)))
        return 202, {}, None

def test_workspace_agent_review_accepts_202_without_provider_run_identity():
    http=EmptyAcceptedHTTP(); dispatcher=pr_lifecycle.WorkspaceAgentDispatcher(access_token="secret",review_trigger_id="agtch_review",http=http)
    result=dispatcher.dispatch(repository="marcogallotta/ai-tools",pr_number=31,pr_url="https://github.com/marcogallotta/ai-tools/pull/31",head=HEAD,review_class="substantive",task_ids=["1217443403986570"])
    assert result.accepted is True and result.status_code==202
    assert result.run_id is None and result.conversation_url is None

def test_worker_dispatch_binds_role_phase_context_and_accepts_empty_202():
    http=EmptyAcceptedHTTP(); dispatcher=pr_lifecycle.WorkspaceAgentDispatcher(access_token="secret",review_trigger_id="agtch_review",worker_trigger_id="agtch_worker",http=http)
    context={"task":"1217513382665760","pr":140,"head":HEAD,"review_id":"4950000000"}
    result=dispatcher.dispatch_worker(role="Implementation",phase="fix",exact_context=context)
    assert result.accepted is True and result.status_code==202 and result.run_id is None
    method,url,headers,body=http.calls[0]
    assert method=="POST" and url.endswith("/workspace_agents/agtch_worker/trigger")
    assert headers["Idempotency-Key"]==dispatcher.worker_idempotency_key(role="Implementation",phase="fix",exact_context=context)
    assert "not a union role" in body["input"] and "Integration landing remains outside Worker authority" in body["input"]
    assert HEAD in body["input"] and "1217513382665760" in body["input"]

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


def test_restart_advisory_integration_lease_does_not_replace_local_fence(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = FakeGitHub()
    gh.reviews = [review()]
    gh.comments = [lease_comment(phase="integration")]
    lifecycle = engine(gh, authority=True)
    lifecycle.local_integration_launcher = FakeLocalIntegration(gh, outcome="merge")
    initial = lifecycle.inspect(gh.pr)
    assert initial.state == pr_lifecycle.LifecycleState.MERGING
    result = lifecycle.dispatch_one(initial, workspace=None, local_reviewer=None)
    assert result.state == pr_lifecycle.LifecycleState.MERGED


def test_foreign_advisory_integration_lease_is_visibility_only_under_v1a(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    gh = FakeGitHub()
    gh.reviews = [review()]
    foreign = lease_comment(phase="integration")
    foreign["body"] = foreign["body"].replace("owner=pr-lifecycle", "owner=another-integrator")
    gh.comments = [foreign]
    lifecycle = engine(gh, authority=True)
    lifecycle.local_integration_launcher = FakeLocalIntegration(gh, outcome="merge")
    result = lifecycle.dispatch_one(lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None)
    assert result.state == pr_lifecycle.LifecycleState.MERGED


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


def test_new_review_phase_metadata_separates_preintegration_tests_from_postmerge_gates():
    gh = FakeGitHub()
    gh.reviews = [
        review(
            body_tail=(
                "PRE-INTEGRATION TESTS TO RUN: NONE.\n"
                "POST-MERGE GATES: task 1217484567901049 — dual-stack TEST qualification before PROD"
            )
        )
    ]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.INTEGRATION_READY
    assert state.local_work == []
    assert state.post_merge_gates == [
        "task 1217484567901049 — dual-stack TEST qualification before PROD"
    ]


def test_new_review_phase_metadata_preintegration_test_still_requires_local_certification():
    gh = FakeGitHub()
    command = "dish/scripts/dish-pg-native-certification --candidate aaaaa"
    gh.reviews = [
        review(
            body_tail=(
                f"PRE-INTEGRATION TESTS TO RUN: {command}\n"
                "POST-MERGE GATES: NONE."
            )
        )
    ]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.LOCAL_CERTIFICATION_REQUIRED
    assert state.local_work[0]["instruction"] == command
    assert state.post_merge_gates == []


def test_partially_new_review_phase_metadata_fails_closed_without_legacy_fallback():
    gh = FakeGitHub()
    gh.reviews = [
        review(
            body_tail=(
                "PRE-INTEGRATION TESTS TO RUN: NONE.\n"
                "TESTS TO RUN: dish/scripts/dish-pg-native-certification --candidate aaaaa"
            )
        )
    ]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.REVIEW_PASSED
    assert "must contain both" in state.residual_reason
    assert state.local_work == []


def test_legacy_tests_to_run_review_remains_preintegration_certification():
    gh = FakeGitHub()
    command = "dish/scripts/dish-pg-native-certification --candidate aaaaa"
    gh.reviews = [review(body_tail=f"TESTS TO RUN: {command}")]
    state = engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.LOCAL_CERTIFICATION_REQUIRED
    assert state.local_work[0]["instruction"] == command
    assert state.post_merge_gates == []
