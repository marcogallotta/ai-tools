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
