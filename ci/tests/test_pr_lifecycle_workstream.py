from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("pr_lifecycle", SCRIPTS / "pr_lifecycle.py")
assert SPEC and SPEC.loader
pr_lifecycle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pr_lifecycle
SPEC.loader.exec_module(pr_lifecycle)

import pr_lifecycle_workstream as workstream


NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
WORKSTREAM_TASK = "1217513381744783"
NUMBERS = [151, 157, 159, 160]
TASKS = [
    "1217545391806442",
    "1217561810880370",
    "1217513443918218",
    "1217519197662916",
]
HEADS = [
    "6731669d731af424578001c66d8b61691301d96d",
    "c7a907ec9f09d4ddddcc6fb74222283bc2069fcf",
    "6962119bf1fe490ef5b30366a24dd7af9e8d5630",
    "7af143a9366905b46a2859cb50d59b0344f0f7d0",
]
BRANCHES = [
    "agent/pr-lifecycle-controller",
    "agent/pr-lifecycle-forward-fix",
    "agent/pr-lifecycle-dashboard",
    "agent/pr-lifecycle-rollout-v2",
]
BASES = ["main", *BRANCHES[:-1]]


def stack_pr(index: int, *, head: str | None = None, total: int = 4) -> dict:
    number = NUMBERS[index]
    return {
        "number": number,
        "html_url": f"https://github.com/marcogallotta/ai-tools/pull/{number}",
        "title": f"Lifecycle stage {index + 1}",
        "state": "open",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "body": (
            f"Owning task: {TASKS[index]}\n"
            f"<!-- dish-workstream:v1 task={WORKSTREAM_TASK} slot={index + 1} total={total} -->"
        ),
        "head": {"sha": head or HEADS[index], "ref": BRANCHES[index]},
        "base": {"ref": BASES[index], "sha": "e" * 40},
        "mergeable": True,
        "mergeable_state": "clean",
    }


class MultiGitHub:
    repository = "marcogallotta/ai-tools"

    def __init__(self, *, count: int = 4):
        self.prs = {NUMBERS[index]: stack_pr(index) for index in range(count)}
        self.comments = {number: [] for number in self.prs}
        self.reviews = {number: [] for number in self.prs}
        self.events = []
        self.pr_files = {number: [] for number in self.prs}

    def list_prs(self, *, include_closed=False):
        return [
            deepcopy(self.prs[number])
            for number in sorted(self.prs)
            if include_closed or self.prs[number]["state"] == "open"
        ]

    def closed_recovery_candidate(self, *, recovery_slot):
        return None

    def get_pr(self, number):
        return deepcopy(self.prs[number])

    def get_pr_files(self, number):
        return deepcopy(self.pr_files[number])

    def get_comments(self, number):
        return deepcopy(self.comments[number])

    def get_reviews(self, number):
        return deepcopy(self.reviews[number])

    def get_combined_status(self, sha):
        index = list(self.prs).index(next(number for number, value in self.prs.items() if value["head"]["sha"] == sha))
        return {
            "sha": sha,
            "statuses": [
                {
                    "context": "Dish / exact-head certification",
                    "state": "success",
                    "updated_at": "2026-08-18T09:10:00Z",
                    "target_url": f"https://github.com/marcogallotta/ai-tools/actions/runs/{700 + index}",
                }
            ],
        }

    def get_workflow_runs(self):
        return {
            "workflow_runs": [
                {
                    "id": 700 + index,
                    "run_attempt": 1,
                    "path": ".github/workflows/ci.yml",
                    "event": "pull_request_review",
                    "pull_requests": [{"number": number}],
                    "status": "completed",
                    "conclusion": "success",
                    "run_started_at": "2026-08-18T09:05:00Z",
                }
                for index, number in enumerate(sorted(self.prs))
            ]
        }

    def add_comment(self, number, body):
        item = {
            "id": len(self.comments[number]) + 1,
            "body": body,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
        self.comments[number].append(item)
        self.events.append(("comment", number, body))
        return deepcopy(item)


class RecordingHTTP:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url, dict(headers or {}), deepcopy(body)))
        return 202, {}, None


class RecordingWorkspace:
    access_token = "secret"
    api_root = "https://api.chatgpt.com/v1"

    def __init__(self):
        self.http = RecordingHTTP()

    def trigger_id_for(self, review_class):
        return "agtch_review"


class RecordingFixer:
    def __init__(self, github: MultiGitHub, *, change_pr: int | None = None):
        self.github = github
        self.change_pr = change_pr
        self.calls = []

    def command_for(self, host):
        assert host == "CHATGPT_IMPLEMENTATION"
        return "fake-chatgpt-implementation"

    def dispatch(self, context, *, host):
        self.calls.append(deepcopy(context))
        if self.change_pr is not None:
            self.github.prs[self.change_pr]["head"]["sha"] = "d" * 40


def engine(github):
    return pr_lifecycle.LifecycleEngine(github, now=lambda: NOW)


def candidate_for(lifecycle, github):
    values = [lifecycle.inspect(github.get_pr(number)) for number in sorted(github.prs)]
    return lifecycle._workstream_candidates(values)[WORKSTREAM_TASK]


def add_workstream_reviews(github, candidate, verdict):
    for member in candidate.members:
        if member.publication_state != "open":
            continue
        github.reviews[member.pr_number].append(
            {
                "id": 1000 + member.pr_number,
                "state": "COMMENTED",
                "commit_id": member.head,
                "submitted_at": NOW.isoformat(),
                "body": (
                    f"VERDICT: {verdict}\n"
                    f"<!-- dish-workstream-review:v1 workstream={WORKSTREAM_TASK} "
                    f"candidate={candidate.candidate_id} shape={candidate.shape_id} -->\n"
                    "TESTS TO RUN: NONE."
                ),
            }
        )


def add_individual_reviews_without_workstream_marker(github):
    for index, number in enumerate(NUMBERS):
        github.reviews[number].append(
            {
                "id": 2000 + number,
                "state": "COMMENTED",
                "commit_id": HEADS[index],
                "submitted_at": NOW.isoformat(),
                "body": "VERDICT: MERGE\nTESTS TO RUN: NONE.",
            }
        )


def test_151_157_159_160_fixture_dispatches_once_with_all_exact_heads_and_order():
    github = MultiGitHub()
    lifecycle = engine(github)
    workspace = RecordingWorkspace()

    values = lifecycle.dispatch(workspace=workspace, local_reviewer=None)

    assert len(workspace.http.calls) == 1
    prompt = workspace.http.calls[0][3]["input"]
    for number, head in zip(NUMBERS, HEADS):
        assert str(number) in prompt
        assert head in prompt
    assert "ONE human/agent Review dispatch" in prompt
    assert [value.number for value in values] == NUMBERS
    assert all(value.state == pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS for value in values)
    for number in NUMBERS:
        assert any("dish-workstream-review-dispatch:v1" in item["body"] for item in github.comments[number])

    lifecycle.dispatch(workspace=workspace, local_reviewer=None)
    assert len(workspace.http.calls) == 1


def test_prior_per_pr_reviews_do_not_satisfy_the_single_workstream_review():
    github = MultiGitHub()
    add_individual_reviews_without_workstream_marker(github)
    lifecycle = engine(github)
    workspace = RecordingWorkspace()

    values = lifecycle.dispatch(workspace=workspace, local_reviewer=None)

    assert len(workspace.http.calls) == 1
    assert all(value.state == pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS for value in values)


def test_block_routes_one_consolidated_fix_and_changed_pr2_gets_one_focused_recheck():
    github = MultiGitHub()
    lifecycle = engine(github)
    candidate = candidate_for(lifecycle, github)
    add_workstream_reviews(github, candidate, "BLOCK")
    fixer = RecordingFixer(github, change_pr=157)

    blocked = lifecycle.dispatch(
        workspace=RecordingWorkspace(),
        local_reviewer=None,
        implementation_fixer=fixer,
    )

    assert len(fixer.calls) == 1
    context = fixer.calls[0]
    assert context["schema"] == "dish-workstream-fix-dispatch-v1"
    assert [member["pr_number"] for member in context["members"]] == NUMBERS
    assert all(value.number in NUMBERS for value in blocked)
    assert github.prs[157]["head"]["sha"] == "d" * 40

    workspace = RecordingWorkspace()
    rereview = lifecycle.dispatch(workspace=workspace, local_reviewer=None, implementation_fixer=fixer)

    assert len(workspace.http.calls) == 1
    prompt = workspace.http.calls[0][3]["input"]
    assert "Changed PRs: [157]" in prompt
    assert "narrow re-review" in prompt
    assert all(value.state == pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS for value in rereview)


def test_passed_workstream_releases_each_member_to_existing_per_pr_lifecycle():
    github = MultiGitHub()
    lifecycle = engine(github)
    candidate = candidate_for(lifecycle, github)
    add_workstream_reviews(github, candidate, "MERGE")
    dispatched = []

    class RecordingEngine(pr_lifecycle.LifecycleEngine):
        def dispatch_one(self, value, **kwargs):
            dispatched.append(value.number)
            return self.inspect(self.github.get_pr(value.number))

    recording = RecordingEngine(github, now=lambda: NOW)
    values = recording.dispatch(workspace=RecordingWorkspace(), local_reviewer=None)

    assert dispatched == NUMBERS
    assert all(value.review_verdict == "MERGE" for value in values)
    assert not any(value.state in {pr_lifecycle.LifecycleState.REVIEW_READY, pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS} for value in values)


def test_incomplete_publication_shape_never_dispatches_review():
    github = MultiGitHub(count=3)
    lifecycle = engine(github)
    workspace = RecordingWorkspace()

    values = lifecycle.dispatch(workspace=workspace, local_reviewer=None)

    assert workspace.http.calls == []
    assert all(value.state == pr_lifecycle.LifecycleState.REVIEW_READY for value in values)
    assert all("not review-complete" in (value.residual_reason or "") for value in values)


def test_reviewed_stack_survives_per_pr_integration_and_later_head_change_is_one_narrow_rereview():
    github = MultiGitHub()
    lifecycle = engine(github)
    workspace = RecordingWorkspace()

    lifecycle.dispatch(workspace=workspace, local_reviewer=None)
    assert len(workspace.http.calls) == 1
    candidate = candidate_for(lifecycle, github)
    add_workstream_reviews(github, candidate, "MERGE")

    # Integration lands PR1 independently. Ordinary open-PR status no longer lists it,
    # but the surviving members retain the exact workstream dispatch context.
    github.prs[151]["state"] = "closed"
    github.prs[151]["merged"] = True
    github.prs[151]["merged_at"] = NOW.isoformat()

    after_first_merge = lifecycle.dispatch(workspace=workspace, local_reviewer=None)
    assert [value.number for value in after_first_merge] == [157, 159, 160]
    assert all(value.review_verdict == "MERGE" for value in after_first_merge)
    assert not any(
        value.state in {pr_lifecycle.LifecycleState.REVIEW_READY, pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS}
        for value in after_first_merge
    )
    assert len(workspace.http.calls) == 1

    # A later member head moves. Review is dispatched once for the workstream; the
    # already-merged predecessor is interaction context, not a fresh review target.
    github.prs[157]["head"]["sha"] = "d" * 40
    rereview_workspace = RecordingWorkspace()
    rereview = lifecycle.dispatch(workspace=rereview_workspace, local_reviewer=None)

    assert len(rereview_workspace.http.calls) == 1
    prompt = rereview_workspace.http.calls[0][3]["input"]
    assert "Changed PRs: [157]" in prompt
    assert '"publication_state": "landed"' in prompt
    assert "do not try to submit a new review to either closed PR" in prompt
    assert all(value.state == pr_lifecycle.LifecycleState.REVIEW_IN_PROGRESS for value in rereview)

    current = lifecycle._workstream_candidates(
        [lifecycle.inspect(github.get_pr(number)) for number in (157, 159, 160)]
    )[WORKSTREAM_TASK]
    assert current.complete is True
    assert current.members[0].pr_number == 151
    assert current.members[0].publication_state == "landed"
    add_workstream_reviews(github, current, "MERGE")
    passed = lifecycle.status()
    assert all(value.review_verdict == "MERGE" for value in passed)
