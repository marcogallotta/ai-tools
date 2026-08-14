from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ACTION = ROOT / ".github" / "actions" / "upload-test-evidence" / "action.yml"


def test_ci_certification_artifacts_are_hidden_path_safe_and_fail_closed():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action = ACTION.read_text(encoding="utf-8")

    assert "uses: ./.github/actions/run-certification" in workflow
    assert workflow.count("uses: actions/upload-artifact@v7") == 2
    assert "if-no-files-found: warn" not in workflow
    assert workflow.count("include-hidden-files: true") == 2
    assert workflow.count("if-no-files-found: error") == 2
    assert "path: .test-artifacts/pr-certification/evidence.json" in workflow

    attempt = "-${{ github.run_id }}-${{ github.run_attempt }}"
    assert f"pr-certification-plan-${{{{ steps.prepare.outputs.candidate_sha }}}}{attempt}" in workflow
    assert f"pr-certification-plan-${{{{ needs.plan.outputs.candidate_sha }}}}{attempt}" in workflow
    assert f"pr-certification-evidence-${{{{ needs.plan.outputs.candidate_sha }}}}{attempt}" in workflow
    assert "overwrite: true" not in workflow

    # A passing test execution is insufficient: durable artifact creation must also succeed
    # and expose an addressable artifact before the terminal success status can be published.
    assert "id: evidence-upload" in workflow
    assert "EVIDENCE_UPLOAD_OUTCOME: ${{ steps.evidence-upload.outcome }}" in workflow
    assert "EVIDENCE_ARTIFACT_ID: ${{ steps.evidence-upload.outputs.artifact-id }}" in workflow
    assert "EVIDENCE_ARTIFACT_URL: ${{ steps.evidence-upload.outputs.artifact-url }}" in workflow
    assert '"$EVIDENCE_UPLOAD_OUTCOME" == "success"' in workflow
    assert '-n "$EVIDENCE_ARTIFACT_ID"' in workflow
    assert '-n "$EVIDENCE_ARTIFACT_URL"' in workflow
    assert "description='Certification evidence publication failed'" in workflow
    assert 'target_url="$EVIDENCE_ARTIFACT_URL"' in workflow

    # Legacy lane uploads remain fail-closed for workflows that still use this action.
    assert "uses: actions/upload-artifact@v7" in action
    assert "include-hidden-files: true" in action
    assert "if-no-files-found: error" in action
    assert "name: ${{ inputs.name }}" in action
    assert "path: ${{ inputs.path }}" in action
