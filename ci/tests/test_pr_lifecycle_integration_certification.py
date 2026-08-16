from __future__ import annotations

from copy import deepcopy

from test_pr_lifecycle import HEAD, FakeGitHub, engine, review


class FakeIntegrationCertifier:
    command = "fake-integration-certifier"

    def __init__(self, github: FakeGitHub, *, complete: bool) -> None:
        self.github = github
        self.complete = complete
        self.calls = []

    def dispatch(self, context):
        self.calls.append(deepcopy(context))
        if self.complete:
            head = context["pull_request"]["head"]
            self.github.add_comment(
                context["pull_request"]["number"],
                f"<!-- dish-local-completion:v1 kind=certification head={head} result=pass -->\n"
                "Local Integration certification passed.",
            )


def test_integration_executes_durable_local_certification_handoff_and_continues_gates():
    gh = FakeGitHub()
    command = "dish/scripts/dish-pg-native-certification --candidate aaaaa"
    gh.reviews = [review(body_tail=f"TESTS TO RUN: {command}")]
    certifier = FakeIntegrationCertifier(gh, complete=True)
    notices = []
    lifecycle = engine(gh, authority=True)

    initial = lifecycle.inspect(gh.pr)
    lifecycle.local_integration_launcher = certifier
    result = lifecycle.dispatch_one(
        initial,
        workspace=None,
        local_reviewer=None,
        notify=notices.append,
    )

    assert len(certifier.calls) == 1
    context = certifier.calls[0]
    assert context["schema"] == "dish-pr-integration-certification-v1"
    assert context["pull_request"]["head"] == HEAD
    assert context["pull_request"]["branch"] == "agent/test"
    assert context["task_ids"] == ["1217443403986570"]
    assert context["local_certification"]["handoff_present"] is True
    assert context["local_certification"]["instruction"] == command
    assert notices == []
    assert not any("dish-human-notice:v1" in event[1] for event in gh.events if event[0] == "comment")
    assert result.state.value == "integration_ready"
    assert not any(event[0] == "merge" for event in gh.events)


def test_integration_certifier_without_durable_completion_does_not_bounce_to_human():
    gh = FakeGitHub()
    command = "dish/scripts/dish-pg-native-certification --candidate aaaaa"
    gh.reviews = [review(body_tail=f"TESTS TO RUN: {command}")]
    certifier = FakeIntegrationCertifier(gh, complete=False)
    notices = []
    lifecycle = engine(gh, authority=True)

    lifecycle.local_integration_launcher = certifier
    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr),
        workspace=None,
        local_reviewer=None,
        notify=notices.append,
    )

    assert len(certifier.calls) == 1
    assert result.state.value == "local_certification_required"
    assert "without durable exact-head completion evidence" in (result.residual_reason or "")
    assert result.human_action is None
    assert notices == []
    assert not any("dish-human-notice:v1" in event[1] for event in gh.events if event[0] == "comment")
