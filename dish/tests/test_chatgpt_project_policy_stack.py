import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "dish/docs/chatgpt-projects/source.json"
EVALS = ROOT / "dish/docs/chatgpt-projects/evals.json"

def _source():
    return json.loads(SOURCE.read_text())

def _rule(source, role, rule_id):
    rules = source["shared_rules"] + source["roles"][role]["rules"]
    return next(rule for rule in rules if rule["id"] == rule_id)

def _scenario(scenario_id):
    return next(x for x in json.loads(EVALS.read_text())["scenarios"] if x["id"] == scenario_id)


def test_bundle_gate_preserves_live_authority_and_behavior_eval():
    source = _source()
    for role in source["roles"]:
        text = _rule(source, role, "live-authority")["text"]
        assert "repository-bundle-<SHA>" in text
        assert "exact-current-main" in text
        assert "Context only" in text
        assert "current-state/ownership/process/dispatch/completion" in text
        assert "live GitHub/Asana reads" in text
    root = (ROOT / "CLAUDE.md").read_text()
    policy = (ROOT / "ci/repository-bundle.md").read_text()
    assert "read-only context" in root and "Asana remains orchestration authority" in root
    assert "read-only context" in policy and "current-state conclusions still require those live reads" in policy
    scenario = _scenario("consequential-reasoning-bundle-live-authority")
    assert set(scenario["roles"]) == set(source["roles"])
    assert {"verify_repository_bundle", "read_live_github_asana_for_current_state"} <= set(scenario["required_actions"])
    assert "treat_bundle_as_live_orchestration_authority" in scenario["forbidden_actions"]


def test_human_review_state_model_is_explicit_and_durable():
    source = _source()
    shared = _rule(source, "coordinator", "decision-provenance")["text"]
    for token in ("HUMAN REVIEW REQUIRED", "NOT REQUIRED", "PENDING/COMPLETE/INADEQUATE", "reviewer identity/provenance", "date/time", "reviewed artifact/PR/head/design", "decision/result"):
        assert token in shared
    coordinator = _rule(source, "coordinator", "durable-review-state")["text"]
    assert "INADEQUATE is distinct from PENDING" in coordinator
    assert "chat/actor/agent claims alone fail" in coordinator
    contract = (ROOT / "dish/docs/agents/coordinator.md").read_text()
    for token in ("HUMAN REVIEW REQUIRED", "HUMAN REVIEW NOT REQUIRED", "`PENDING`", "`COMPLETE`", "`INADEQUATE`", "reviewer identity", "date/time", "PR/head", "decision/result"):
        assert token in contract
    scenario = _scenario("human-review-durable-state-model")
    assert "record_human_review_required_or_not_required" in scenario["required_actions"]
    assert "collapse_inadequate_into_pending" in scenario["forbidden_actions"]


def test_fresh_sessions_reconcile_queue_and_audit_before_dispatch():
    source = _source()
    coordinator = _rule(source, "coordinator", "coordinator-live-scan")
    workflow = _rule(source, "development-workflow", "development-workflow-friction-triage")
    for current in (coordinator, workflow):
        text = current["text"]
        assert "Fresh/replacement" in text
        assert "before ordinary status/dispatch" in text
        assert "audit due/active/incomplete/returned state" in text
        assert "surface due audits before next work" in text
        assert "no scheduler/second queue" in text
        assert {"status", "dispatch"} <= set(current["action_boundaries"])
    coordinator_contract = (ROOT / "dish/docs/agents/coordinator.md").read_text()
    workflow_contract = (ROOT / "dish/docs/agents/development-workflow.md").read_text()
    for contract in (coordinator_contract, workflow_contract):
        assert "before ordinary status conclusions, next-work selection, or dispatch" in contract
        assert "due-but-unsent, active, incomplete, or returned audits" in contract
        assert "fast path narrow unless drift is detected" in contract
    for scenario_id in ("coordinator-fresh-session-queue-audit-reconciliation", "development-workflow-fresh-session-queue-audit-reconciliation"):
        scenario = _scenario(scenario_id)
        assert "only_then_select_or_dispatch_next_work" in scenario["required_actions"]
        assert "dispatch_before_queue_audit_reconciliation" in scenario["forbidden_actions"]
