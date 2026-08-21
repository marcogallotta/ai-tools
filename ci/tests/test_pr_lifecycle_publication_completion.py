from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess

import pytest

import test_pr_lifecycle as base
from pr_lifecycle_publication_completion import (
    DIRECT_CONNECTOR,
    EXACT_BYTE_HANDOFF,
    EXACT_BYTE_ARTIFACT_PUBLICATION,
    FRESH_AUTHORING_REQUIRED,
    PUBLICATION_BLOCKER_HEADING,
    classify_publication_route,
    classify_receiver_bundle,
    render_local_publication_handoff,
    render_publication_fallback_notice,
)

HEAD = base.HEAD
TREE = "d" * 40


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def _candidate_bundle(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "candidate.txt").write_text("exact candidate bytes\n", encoding="utf-8")
    _git(repo, "add", "candidate.txt")
    _git(repo, "commit", "-m", "candidate")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    bundle = downloads / "candidate.bundle"
    _git(repo, "bundle", "create", str(bundle), "main")
    return bundle, head, tree


def test_publication_route_requires_connector_attempt_before_local_fallback_even_for_large_multifile_candidate():
    result = classify_publication_route(
        connector_attempt_state="not-attempted",
        exact_candidate_bytes_available=True,
    )
    assert result.route == DIRECT_CONNECTOR
    assert "must be attempted first" in result.reason
    assert "size or file count alone" in result.reason
    assert result.exact_byte_receiver_verification_required is False


def test_publication_route_keeps_working_connector_remote():
    result = classify_publication_route(
        connector_attempt_state="working",
        exact_candidate_bytes_available=True,
    )
    assert result.route == DIRECT_CONNECTOR


@pytest.mark.parametrize("state", ["failing", "slow-or-manual", "unavailable"])
def test_publication_route_uses_single_bundle_only_after_observed_connector_failure_or_degradation(state: str):
    result = classify_publication_route(
        connector_attempt_state=state,
        exact_candidate_bytes_available=True,
        attempted_actions=("GitHub connector update_file on candidate path",),
        stop_reason="connector mutation failed or degraded into manual blob transport",
    )
    assert result.route == EXACT_BYTE_ARTIFACT_PUBLICATION
    assert result.exact_byte_receiver_verification_required is True
    assert "attempted GitHub connector path" in result.reason
    assert result.attempted_actions == ("GitHub connector update_file on candidate path",)
    assert result.stop_reason == "connector mutation failed or degraded into manual blob transport"


def test_publication_route_rejects_local_fallback_without_attempts_and_stop_reason():
    with pytest.raises(base.pr_lifecycle.LifecycleError, match="requires at least one concrete GitHub connector publication attempt"):
        classify_publication_route(
            connector_attempt_state="failing",
            exact_candidate_bytes_available=True,
        )


def test_publication_fallback_notice_tells_marco_why_and_what_was_tried():
    route = classify_publication_route(
        connector_attempt_state="failing",
        exact_candidate_bytes_available=True,
        attempted_actions=("update_file scripts/example.py", "create_pull_request"),
        stop_reason="update_file transport returned a payload failure",
    )
    notice = render_publication_fallback_notice(route)
    assert "Stopped GitHub connector publication because: update_file transport returned a payload failure" in notice
    assert "Tried: update_file scripts/example.py; create_pull_request" in notice
    assert "Next route: EXACT-BYTE ARTIFACT/BUNDLE PUBLICATION" in notice


def test_publication_route_requires_fresh_authoring_if_attempt_failed_and_exact_bytes_are_lost():
    result = classify_publication_route(
        connector_attempt_state="failing",
        exact_candidate_bytes_available=False,
        attempted_actions=("GitHub connector update_file on candidate path",),
        stop_reason="connector mutation failed",
    )
    assert result.route == FRESH_AUTHORING_REQUIRED
    assert result.prior_exact_tree_evidence_transferable is False


def test_hashes_or_prose_without_receiver_bundle_require_fresh_authoring(tmp_path: Path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "candidate.bundle.sha256").write_text("deadbeef\n", encoding="utf-8")
    result = classify_receiver_bundle(
        downloads_dir=downloads,
        bundle_filename="candidate.bundle",
        expected_head=HEAD,
        expected_tree=TREE,
    )
    assert result.status == FRESH_AUTHORING_REQUIRED
    assert result.prior_evidence_transferable is False
    assert "sidecars cannot substitute for bytes" in result.reason


def test_single_receiver_readable_bundle_allows_exact_byte_handoff_and_ignores_extras(tmp_path: Path):
    bundle, head, tree = _candidate_bundle(tmp_path)
    downloads = bundle.parent
    # These are intentionally irrelevant. The receiver must not require or interpret them.
    (downloads / "candidate.bundle.sha256").write_text("wrong and irrelevant\n", encoding="utf-8")
    (downloads / "candidate.manifest.json").write_text("not required\n", encoding="utf-8")
    (downloads / "holiday-photo.txt").write_text("ignore me\n", encoding="utf-8")

    result = classify_receiver_bundle(
        downloads_dir=downloads,
        bundle_filename=bundle.name,
        expected_head=head,
        expected_tree=tree,
    )

    assert result.status == EXACT_BYTE_HANDOFF
    assert result.allowed is True
    assert result.observed_tree == tree
    assert result.prior_evidence_transferable is True


def test_short_local_handoff_requires_only_bundle_in_downloads_and_immediate_start():
    text = render_local_publication_handoff(
        task_gid="1217482679284514",
        pr_url="https://github.com/marcogallotta/ai-tools/pull/192",
        branch="agent/example",
        bundle_filename="candidate.bundle",
        expected_tree=TREE,
    )
    assert len(text.splitlines()) == 4
    assert "Start now; do not pause for confirmation" in text
    assert "Use only `~/Downloads/candidate.bundle`" in text
    assert "bundle is the only download you need" in text
    assert "do not stop because `.sha256`, manifest, or other sidecars are absent" in text
    assert "ignore" in text.lower()


def test_temporary_containment_contract_forbids_broken_bundle_card_and_requires_stop_explanation():
    repo_root = Path(__file__).resolve().parents[2]
    implementation = (repo_root / "dish/docs/agents/implementation.md").read_text(encoding="utf-8")
    local_handoff = (repo_root / "tools/agent-worktree-handoff.md").read_text(encoding="utf-8")
    for text in (implementation, local_handoff):
        assert "GitHub connector" in text
        assert "artifact-card" in text
        assert "sandbox-link/card" in text
        assert "reported as non-working" in text
        assert "what" in text.lower() and "tried" in text.lower()
    assert "why it stopped and exactly what was tried" in implementation
    assert "working directly downloadable file/attachment surface" in implementation


class FinalizerGitHub(base.FakeGitHub):
    def __init__(self, candidate=None, *, ready_mutates=True, body_update_mutates=True):
        candidate = deepcopy(candidate or base.pr(draft=True))
        candidate.setdefault("node_id", "PR_test")
        super().__init__(candidate)
        self.ready_mutates = ready_mutates
        self.body_update_mutates = body_update_mutates

    def update_pr_body(self, number, body):
        self.events.append(("update-pr-body", body))
        if self.body_update_mutates:
            self.pr["body"] = body
        return deepcopy(self.pr)

    def mark_ready_for_review(self, number):
        self.events.append(("mark-ready", self.pr["head"]["sha"]))
        if self.ready_mutates:
            self.pr["draft"] = False
        return deepcopy(self.pr)


def test_complete_implementation_draft_finalizer_marks_same_pr_ready_and_reads_back():
    gh = FinalizerGitHub(base.pr(draft=True, body="Owning task: 1217482679284514\nFocused evidence: PASS\n"))
    lifecycle = base.engine(gh)

    result = lifecycle.finalize_implementation_pr(31, expected_head=HEAD)

    assert result["complete"] is True
    assert result["after"]["head"] == HEAD
    assert result["after"]["draft"] is False
    assert [event[0] for event in gh.events] == ["mark-ready"]


def test_pr_192_shape_cannot_report_review_next_while_live_pr_remains_draft():
    gh = FinalizerGitHub(base.pr(draft=True), ready_mutates=False)
    result = base.engine(gh).finalize_implementation_pr(31, expected_head=HEAD)
    assert result["complete"] is False
    assert result["after"]["draft"] is True
    assert "remains draft" in result["reason"]


def test_partial_publication_head_mismatch_cannot_finalize_complete_tested_candidate():
    gh = FinalizerGitHub(base.pr(head=base.NEW_HEAD, draft=True))
    result = base.engine(gh).finalize_implementation_pr(31, expected_head=HEAD)
    assert result["complete"] is False
    assert "exact head moved" in result["reason"]
    assert not any(event[0] == "mark-ready" for event in gh.events)


def test_current_authoring_blocker_or_explicit_keep_draft_prevents_ready_transition():
    gh = FinalizerGitHub(
        base.pr(
            draft=True,
            body="Owning task: 1217482679284514\nIMPLEMENTATION EVIDENCE PENDING: focused smoke\n",
        )
    )
    result = base.engine(gh).finalize_implementation_pr(31, expected_head=HEAD)
    assert result["complete"] is False
    assert "focused smoke" in result["reason"]
    assert not gh.events

    gh = FinalizerGitHub(base.pr(draft=True))
    result = base.engine(gh).finalize_implementation_pr(
        31,
        expected_head=HEAD,
        keep_draft_reason="explicit handoff says preserve draft",
    )
    assert result["complete"] is False
    assert "explicit keep-draft" in result["reason"]
    assert not gh.events


def test_local_completion_clears_stale_publication_blocker_then_marks_ready():
    body = (
        "Owning task: 1217482679284514\n\n"
        f"{PUBLICATION_BLOCKER_HEADING}\n"
        "State: LOCAL IMPLEMENTATION COMPLETION REQUIRED\n"
        "Exact unpublished candidate is transferred separately.\n\n"
        "## Review context\nFocused evidence: PASS\n"
    )
    gh = FinalizerGitHub(base.pr(draft=True, body=body))
    result = base.engine(gh).finalize_implementation_pr(
        31,
        expected_head=HEAD,
        clear_publication_blocker=True,
    )
    assert result["complete"] is True
    assert [event[0] for event in gh.events] == ["update-pr-body", "mark-ready"]
    assert PUBLICATION_BLOCKER_HEADING not in gh.pr["body"]
    assert "## Review context" in gh.pr["body"]


def test_ready_transport_exception_keeps_completion_unfinished():
    class BrokenReadyGitHub(FinalizerGitHub):
        def mark_ready_for_review(self, number):
            raise base.pr_lifecycle.LifecycleError("GraphQL transport unavailable")

    gh = BrokenReadyGitHub(base.pr(draft=True))
    result = base.engine(gh).finalize_implementation_pr(31, expected_head=HEAD)
    assert result["complete"] is False
    assert "GraphQL transport unavailable" in result["reason"]


def test_ready_transition_or_blocker_clear_readback_failure_keeps_completion_unfinished():
    gh = FinalizerGitHub(base.pr(draft=True), ready_mutates=False)
    result = base.engine(gh).finalize_implementation_pr(31, expected_head=HEAD)
    assert result["complete"] is False

    gh = FinalizerGitHub(
        base.pr(draft=True, body=f"Owning task: 1217482679284514\n{PUBLICATION_BLOCKER_HEADING}\nState: LOCAL IMPLEMENTATION COMPLETION REQUIRED\n"),
        body_update_mutates=False,
    )
    result = base.engine(gh).finalize_implementation_pr(
        31,
        expected_head=HEAD,
        clear_publication_blocker=True,
    )
    assert result["complete"] is False
    assert "did not clear" in result["reason"]
    assert not any(event[0] == "mark-ready" for event in gh.events)


def test_github_ready_for_review_transport_uses_graphql_then_authoritative_rest_readback():
    class HTTP:
        def __init__(self):
            self.calls = []
            self.reads = 0

        def request(self, method, url, *, headers=None, body=None):
            self.calls.append((method, url, body))
            if url.endswith("/pulls/31"):
                self.reads += 1
                return 200, {}, {
                    **base.pr(draft=self.reads < 2),
                    "node_id": "PR_node",
                }
            assert url == "https://api.github.com/graphql"
            assert "markPullRequestReadyForReview" in body["query"]
            assert body["variables"] == {"pullRequestId": "PR_node"}
            return 200, {}, {
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": {"number": 31, "isDraft": False, "headRefOid": HEAD}
                    }
                }
            }

    http = HTTP()
    github = base.pr_lifecycle.GitHubREST("marcogallotta/ai-tools", "token", http=http)
    readback = github.mark_ready_for_review(31)
    assert readback["draft"] is False
    assert [call[0] for call in http.calls] == ["GET", "POST", "GET"]
