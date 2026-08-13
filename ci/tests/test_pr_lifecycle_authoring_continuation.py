import test_pr_lifecycle as base

pr_lifecycle = base.pr_lifecycle


def test_draft_pr_with_unfinished_authoring_evidence_stays_in_implementation_continuation():
    body = (
        "Owning task: 1217450869324199\n"
        "Focused evidence: complete.\n"
        "IMPLEMENTATION EVIDENCE PENDING: required smoke"
    )
    gh = base.FakeGitHub(base.pr(draft=True, body=body))

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == pr_lifecycle.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED
    assert state.authoring_evidence == "required smoke"
    assert state.local_work == []
    assert state.human_action == "PR #31 still needs Implementation to finish required smoke."


def test_draft_without_explicit_missing_evidence_remains_authoring_not_reviewable():
    gh = base.FakeGitHub(base.pr(draft=True))
    state = base.engine(gh).inspect(gh.pr)
    assert state.state == pr_lifecycle.LifecycleState.AUTHORING
