from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _job(workflow: str, name: str, next_name: str) -> str:
    return workflow.split(f"  {name}:\n", 1)[1].split(f"  {next_name}:\n", 1)[0]


def test_cheap_preflight_is_a_hard_dependency_of_all_expensive_lanes() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "  preflight:\n" in workflow
    assert "needs: [bundle-metadata, preflight]" in _job(workflow, "python-tests", "frontend-tooling")
    assert "needs: [bundle-metadata, preflight]" in _job(workflow, "frontend-tooling", "native-postgresql")
    assert "needs: [bundle-metadata, preflight]" in _job(workflow, "native-postgresql", "browser-acceptance")
    assert "needs: [bundle-metadata, preflight]" in _job(workflow, "browser-acceptance", "exact-head-ordinary-ci")
    terminal = _job(workflow, "exact-head-ordinary-ci", "__missing__") if "  __missing__:\n" in workflow else workflow.split("  exact-head-ordinary-ci:\n", 1)[1]
    assert "needs: [preflight, python-tests, frontend-tooling, native-postgresql, browser-acceptance]" in terminal
    assert '"preflight": os.environ["PREFLIGHT_RESULT"]' in terminal


def test_preflight_uses_exact_candidate_diff_and_emits_json_evidence() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    section = _job(workflow, "preflight", "python-tests")

    assert "ref: ${{ env.CI_CANDIDATE_SHA }}" in section
    assert "fetch-depth: 0" in section
    assert 'pull_request) base="${{ github.event.pull_request.base.sha }}"' in section
    assert 'push) base="${{ github.event.before }}"' in section
    assert 'scripts/dish-test-preflight \\' in section
    assert '--base "$base"' in section
    assert "--allow-empty" in section
    assert "--json-output ../.test-artifacts/preflight/result.json" in section
    assert "preflight-${{ env.CI_CANDIDATE_SHA }}" in section


def test_preflight_job_contains_no_expensive_execution_boundary() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    section = _job(workflow, "preflight", "python-tests")

    forbidden = (
        "services:",
        "postgres:17",
        "dish-pg-native-certification",
        "playwright",
        "test:acceptance",
        "frontend/tests/browser",
        "-m pytest --smoke",
        "-m pytest --database-boundary",
        ".venv/bin/python -m pytest\n",
    )
    for value in forbidden:
        assert value not in section
