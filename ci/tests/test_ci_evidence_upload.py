from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ACTION = ROOT / ".github" / "actions" / "upload-test-evidence" / "action.yml"


def test_ci_routes_all_required_lane_evidence_through_fail_closed_action():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action = ACTION.read_text(encoding="utf-8")

    assert "uses: ./.github/actions/run-certification" in workflow
    assert workflow.count("uses: actions/upload-artifact@v7") == 2
    assert "pr-certification-plan-" in workflow
    assert "pr-certification-evidence-" in workflow
    assert "if-no-files-found: warn" in workflow

    # Legacy lane uploads remain fail-closed for workflows that still use this action.
    assert "uses: actions/upload-artifact@v7" in action
    assert "include-hidden-files: true" in action
    assert "if-no-files-found: error" in action
    assert "name: ${{ inputs.name }}" in action
    assert "path: ${{ inputs.path }}" in action
