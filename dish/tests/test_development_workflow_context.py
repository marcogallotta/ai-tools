from __future__ import annotations

import importlib.util
from pathlib import Path


DISH_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = DISH_ROOT / "scripts" / "chatgpt_project_kernels.py"
AGENT_DIR = DISH_ROOT / "docs" / "agents"

SPEC = importlib.util.spec_from_file_location("chatgpt_project_kernels_context", SCRIPT)
assert SPEC and SPEC.loader
kernels = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kernels)


def _development_workflow_kernel() -> str:
    manifest, source = kernels.load_canonical()
    return kernels.render_role(manifest, source, "development-workflow")


def _development_workflow_contract() -> str:
    return (AGENT_DIR / "development-workflow.md").read_text()


def test_fresh_development_workflow_startup_preloads_governed_contracts() -> None:
    text = _development_workflow_kernel()
    required = {
        "coordinator.md",
        "development-workflow.md",
        "implementation.md",
        "review.md",
        "integration.md",
        "workflow.md",
        "postgresql-dark-launch.md",
    }
    assert required <= kernels.role_index_contracts()
    assert "read-only context preload rule" in text
    assert "load every standing role contract listed by the canonical role index" in text
    assert "`contributor-base.md` as read-only decision context" in text


def test_compaction_replacement_regrounds_the_same_context_dependencies() -> None:
    contract = _development_workflow_contract()
    assert "## Governed decision context" in contract
    assert "every standing role contract linked from the canonical [`index.md`](index.md)" in contract
    assert "[`contributor-base.md`](contributor-base.md)" in contract
    compaction = contract.split("## Agent lifecycle and compaction recovery", 1)[1]
    assert "the governed decision-context preload above" in compaction


def test_context_preload_does_not_expand_development_workflow_authority() -> None:
    _, source = kernels.load_canonical()
    role = source["roles"]["development-workflow"]
    assert role["allowed_compositions"] == [
        "When explicitly assigned repository implementation, additionally load `implementation.md`; its lifecycle applies, with no self-review or Integration of the semantic change."
    ]
    text = _development_workflow_kernel()
    assert "never grants their authority" in text
    assert "explicit Implementation composition above is the only existing authority expansion path" in text


def test_pr60_style_test_scope_context_sees_review_and_integration_semantics() -> None:
    kernel = _development_workflow_kernel()
    review = (AGENT_DIR / "review.md").read_text()
    integration = (AGENT_DIR / "integration.md").read_text()
    assert "dish/docs/testing.md" in kernel
    assert "dish/docs/architecture/testing-boundaries.md" in kernel
    assert "Treat implementation-agent test evidence as evidence; rerun only for a concrete review reason." in review
    assert "Missing, pending, or failed ordinary CI is Integration evidence/ownership state" in review
    assert "Run the exact `TESTS TO RUN`" in integration


def test_pr40_style_unavailable_local_certification_sees_contributor_fallback() -> None:
    kernel = _development_workflow_kernel()
    contributor = (AGENT_DIR / "contributor-base.md").read_text()
    assert "`contributor-base.md` as read-only decision context" in kernel
    assert "## Authorized fallback gate" in contributor
    assert "use an equivalent authorized fallback" in contributor


def test_action_specific_context_routes_are_rendered() -> None:
    text = _development_workflow_kernel()
    assert "ci/pr-lifecycle-dispatcher-runbook.md" in text
    assert "dish/docs/architecture/postgresql-runtime.md" in text
