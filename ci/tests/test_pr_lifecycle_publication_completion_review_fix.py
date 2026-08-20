from __future__ import annotations

from copy import deepcopy
import json

import test_pr_lifecycle as base
from pr_lifecycle_publication_completion import FRESH_AUTHORING_REQUIRED

HEAD = base.HEAD


class FinalizerGitHub(base.FakeGitHub):
    def __init__(self, candidate=None, *, ready_mutates=True):
        candidate = deepcopy(candidate or base.pr(draft=True))
        candidate.setdefault("node_id", "PR_test")
        super().__init__(candidate)
        self.ready_mutates = ready_mutates

    def update_pr_body(self, number, body):
        self.events.append(("update-pr-body", body))
        self.pr["body"] = body
        return deepcopy(self.pr)

    def mark_ready_for_review(self, number):
        self.events.append(("mark-ready", self.pr["head"]["sha"]))
        if self.ready_mutates:
            self.pr["draft"] = False
        return deepcopy(self.pr)


def test_fresh_authoring_cli_returns_exit_two_and_classified_payload(capsys):
    code = base.pr_lifecycle.main(
        [
            "classify-publication-route",
            "--connector-attempt-state", "failing",
            "--no-exact-candidate-bytes-available",
            "--attempt", "GitHub connector update_file on candidate path",
            "--stop-reason", "connector mutation failed",
        ]
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["route"] == FRESH_AUTHORING_REQUIRED
    assert payload["prior_exact_tree_evidence_transferable"] is False


def test_installed_host_evidence_blocker_prevents_ready_transition():
    gh = FinalizerGitHub(
        base.pr(draft=True, body="Owning task: 1217482679284514\nFocused evidence: PASS\n")
    )
    gh.pr_files = [{"filename": ".claude/settings.json", "patch": "@@ changed host wiring @@"}]

    result = base.engine(gh).finalize_implementation_pr(31, expected_head=HEAD)

    assert result["complete"] is False
    assert "installed Claude/Codex host certification remains pending" in result["reason"]
    assert "no exact-head installed-host certificate" in result["reason"]
    assert not any(event[0] == "mark-ready" for event in gh.events)


def test_final_readback_closed_pr_cannot_report_review_ready():
    class ClosingGitHub(FinalizerGitHub):
        def __init__(self, candidate=None):
            super().__init__(candidate)
            self.reads = 0

        def get_pr(self, number):
            self.reads += 1
            if self.reads >= 3:
                self.pr["state"] = "closed"
            return super().get_pr(number)

    gh = ClosingGitHub(base.pr(draft=True))
    result = base.engine(gh).finalize_implementation_pr(31, expected_head=HEAD)

    assert result["complete"] is False
    assert result["after"]["state"] == "closed"
    assert "PR is not open" in result["reason"]
