from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "pr-mutation-broker.yml").read_text()


def test_broker_workflow_is_request_only_and_per_pr_serialized():
    assert "issue_comment:" in WORKFLOW
    assert "types: [created]" in WORKFLOW
    assert "dish-mutation-request:v1" in WORKFLOW
    assert "group: dish-pr-mutation-${{ needs.filter.outputs.pr_number }}" in WORKFLOW
    assert "queue: max" in WORKFLOW
    assert "cancel-in-progress" not in WORKFLOW


def test_broker_runs_trusted_default_branch_code_not_pr_head_code():
    assert "ref: ${{ github.event.repository.default_branch }}" in WORKFLOW
    assert 'source_sha="$(gh api' in WORKFLOW
    assert "ref: ${{ steps.authority.outputs.source_sha }}" in WORKFLOW
    assert "github.event.pull_request.head" not in WORKFLOW
    assert "persist-credentials: false" in WORKFLOW


def test_broker_token_has_no_source_merge_or_review_approval_authority():
    broker_permissions = WORKFLOW.split("  broker:", 1)[1].split("    env:", 1)[0]
    assert "actions: read" in broker_permissions
    assert "checks: read" in broker_permissions
    assert "contents: read" in broker_permissions
    assert "issues: write" in broker_permissions
    assert "pull-requests: read" in broker_permissions
    assert "statuses: read" in broker_permissions
    assert "contents: write" not in broker_permissions
    assert "pull-requests: write" not in broker_permissions
    assert "checks: write" not in broker_permissions


def test_every_proven_event_uploads_run_attempt_comment_bound_artifact_before_finalize():
    assert "mutation-broker-proof.json" in WORKFLOW
    assert "retention-days: 7" in WORKFLOW
    assert "steps.prepare.outputs.artifact_name" in WORKFLOW
    assert "steps.upload.outputs.artifact-id" in WORKFLOW
    assert "steps.upload.outputs.artifact-digest" in WORKFLOW
    assert WORKFLOW.index("Upload immutable run-attempt proof") < WORKFLOW.index("Finalize same event comment")
