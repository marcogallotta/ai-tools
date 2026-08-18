from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publication-materializer.yml"


def load_text():
    return WORKFLOW.read_text(encoding="utf-8")


def test_permission_split_and_same_pr_serialization():
    text = load_text()
    assert "issue_comment:" in text
    assert "dish-publication-materialize:v1" in text
    assert text.count("contents: write") == 1
    assert "group: dish-publication-materialize-${{ needs.filter.outputs.pr_number }}" in text
    assert "cancel-in-progress: false" in text
    materialize = text.split("  materialize:", 1)[1].split("  report:", 1)[0]
    report = text.split("  report:", 1)[1]
    assert "contents: write" in materialize
    assert "issues: write" not in materialize
    assert "issues: write" in report
    assert "contents: write" not in report


def test_workflow_never_updates_source_ref_or_merges_candidate():
    text = load_text()
    forbidden = ["git push", "git update-ref", "/git/refs", "/merges", "ready_for_review", "pulls/merge"]
    for needle in forbidden:
        assert needle not in text
    assert "persist-credentials: false" in text
    assert "github.event.pull_request.head.sha" not in text


def test_durable_result_is_uploaded_and_verified_before_report():
    text = load_text()
    materialize = text.split("  materialize:", 1)[1].split("  report:", 1)[0]
    assert "actions/upload-artifact@v4" in materialize
    assert "retention-days: 7" in materialize
    assert "continue-on-error: true" in materialize
    assert "UNRESOLVED_MATERIALIZED_RESULT" in materialize
    assert "verify-result-artifact" in materialize
    assert materialize.index("actions/upload-artifact@v4") < materialize.index("verify-result-artifact")
    assert materialize.index("verify-result-artifact") < text.index("  report:")


def test_report_recovers_from_artifact_not_ephemeral_candidate_outputs():
    text = load_text()
    report = text.split("  report:", 1)[1]
    assert "publish-result" in report
    assert "ARTIFACT_ID" in report
    assert "ARTIFACT_DIGEST" in report
    assert "candidate_commit" not in report
    assert "actions: read" in report
    assert "issues: write" in report
    assert "contents: write" not in report


def test_filter_routes_same_request_to_recovery_and_tracks_run_attempt():
    text = load_text()
    filt = text.split("  filter:", 1)[1].split("  materialize:", 1)[0]
    assert "actions: read" in filt
    assert "--run-attempt \"${{ github.run_attempt }}\"" in filt
    assert "artifact_id:" in filt
    assert "artifact_run_id:" in filt
    assert "route:" in filt
