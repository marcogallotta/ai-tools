from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "code_quality_attestation", ROOT / "scripts" / "code_quality_attestation.py"
)
assert SPEC and SPEC.loader
cqa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cqa)


def _trusted_run(*, event: str = "issue_comment", branch: str = "main", run_id: int = 77):
    return {
        "id": run_id,
        "run_attempt": 1,
        "path": cqa.WORKFLOW_PATH,
        "event": event,
        "head_branch": branch,
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": "marcogallotta/ai-tools"},
    }


def _artifacts(*, run_id: int = 77, head: str = "a" * 40, digest: str = "b" * 64):
    return {
        "artifacts": [{
            "name": cqa.artifact_name(
                pr_number=242,
                head_sha=head,
                result_digest=digest,
                run_id=run_id,
                run_attempt=1,
            ),
            "expired": False,
            "workflow_run": {"id": run_id},
        }]
    }


def test_trusted_issue_comment_attestation_is_authoritative():
    result = cqa.verify_attestation(
        _trusted_run(),
        _artifacts(),
        repository="marcogallotta/ai-tools",
        default_branch="main",
        pr_number=242,
        head_sha="a" * 40,
        result_digest="b" * 64,
    )
    assert result["authoritative"] is True
    assert result["workflow_run_id"] == 77


def test_candidate_pull_request_status_cannot_satisfy_authoritative_predicate():
    forged_status = {"context": "Dish / code quality", "state": "success"}
    assert forged_status["state"] == "success"  # deliberately irrelevant to admission
    try:
        cqa.verify_attestation(
            _trusted_run(event="pull_request", branch="agent/forged"),
            _artifacts(),
            repository="marcogallotta/ai-tools",
            default_branch="main",
            pr_number=242,
            head_sha="a" * 40,
            result_digest="b" * 64,
        )
    except cqa.AttestationError as exc:
        assert "event is not trusted issue_comment" in str(exc)
    else:
        raise AssertionError("candidate pull_request workflow satisfied trusted attestation")


def test_attestation_artifact_binds_exact_head_and_current_attempt():
    try:
        cqa.verify_attestation(
            _trusted_run(),
            _artifacts(head="c" * 40),
            repository="marcogallotta/ai-tools",
            default_branch="main",
            pr_number=242,
            head_sha="a" * 40,
            result_digest="b" * 64,
        )
    except cqa.AttestationError as exc:
        assert "exact-head attestation artifact" in str(exc)
    else:
        raise AssertionError("wrong-head attestation was accepted")

    run = _trusted_run()
    run["run_attempt"] = 2
    try:
        cqa.verify_attestation(
            run,
            _artifacts(),
            repository="marcogallotta/ai-tools",
            default_branch="main",
            pr_number=242,
            head_sha="a" * 40,
            result_digest="b" * 64,
        )
    except cqa.AttestationError as exc:
        assert "exact-head attestation artifact" in str(exc)
    else:
        raise AssertionError("prior-attempt attestation was accepted")


def test_workflow_keeps_status_diagnostic_and_attestation_authoritative():
    text = (ROOT / ".github" / "workflows" / "code-quality.yml").read_text()
    verify, report = text.split("\n  report:\n", 1)
    assert "statuses: write" not in verify
    assert "actions/upload-artifact@v7" in verify
    assert "Build trusted exact-head attestation" in verify
    assert "if: github.event_name == 'issue_comment'" in verify
    assert "statuses: write" in report
    assert "Publish exact-head status without candidate execution" in report
    assert "actions/checkout" not in report
