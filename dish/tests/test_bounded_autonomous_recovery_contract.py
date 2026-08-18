from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE = ROOT / "OPERATOR_CONTROL_PLANE.md"


def _recovery_contract() -> str:
    text = CONTROL_PLANE.read_text()
    start = text.index("### Bounded autonomous recovery")
    end = text.index("\n## TRUE READY dispatch queue", start)
    return text[start:end]


def test_recovery_is_bounded_continuation_not_new_authority() -> None:
    contract = _recovery_contract()
    assert "already-authorized objective" in contract
    assert "continuation problem, not a new operator decision" in contract
    assert "creates no source authority, role composition, scheduler, database, queue, service, or control plane" in contract
    assert "mapped standing role and current host authority remain controlling" in contract


def test_positive_missing_prerequisite_retries_same_operation() -> None:
    contract = _recovery_contract()
    assert "environmental, prerequisite, transient" in contract
    assert "existing supported operation" in contract
    assert "smallest supported causal remediation" in contract
    assert "immediately rerun the same failed operation" in contract
    assert "on PASS, continue the already-authorized objective without interrupting Marco" in contract


def test_positive_transient_read_uses_bounded_retry() -> None:
    contract = _recovery_contract()
    assert "Read-only/idempotent operations may use normal bounded transient retry/backoff" in contract
    assert "same total recovery budget" in contract


def test_positive_disposable_state_recovery_requires_known_good_state() -> None:
    contract = _recovery_contract()
    assert "capture or reuse the supported known-good pre-state/checkpoint" in contract
    assert "reversible or bounded" in contract


def test_positive_ambiguous_write_success_is_not_replayed() -> None:
    contract = _recovery_contract()
    assert "perform authoritative readback/reconciliation before replay" in contract
    assert "If the intended effect is proven present, resume from observed state and **do not replay**" in contract


def test_positive_absent_safe_write_gets_only_bounded_retry() -> None:
    contract = _recovery_contract()
    assert "If absence is proven and replay is safe/idempotent, one retry may proceed" in contract
    assert "remaining budget" in contract


def test_negative_unknown_mutation_outcome_fails_closed() -> None:
    contract = _recovery_contract()
    assert "If the outcome cannot be established or replay could duplicate/compound the mutation, fail closed" in contract


def test_negative_authentication_and_destructive_prod_crossings_stop() -> None:
    contract = _recovery_contract()
    assert "no new credentials/login" in contract
    assert "destructive operation, production mutation" in contract
    assert "credential/login, destructive/PROD" in contract


def test_negative_same_class_cannot_loop() -> None:
    contract = _recovery_contract()
    assert "at most one diagnosed remediation plus one immediate retry" in contract
    assert "never repeat the same unresolved remediation loop" in contract
    assert "same failure persists after its one remediation+retry" in contract


def test_negative_total_fallback_budget_is_two_distinct_cycles() -> None:
    contract = _recovery_contract()
    assert "at most **two distinct automatic recovery cycles**" in contract
    assert "Exhaustion stops deterministically" in contract
    assert "Do not evade either bound by relabeling an unresolved failure" in contract


def test_negative_new_class_requires_forward_progress() -> None:
    contract = _recovery_contract()
    assert "genuinely distinct newly exposed failure class" in contract
    assert "prior cycle made demonstrable forward progress" in contract
    assert "alleged next class is not genuinely new or prior recovery made no forward progress" in contract


def test_negative_moved_candidate_or_ambiguous_semantic_fix_stops() -> None:
    contract = _recovery_contract()
    assert "candidate/head/target moved" in contract
    assert "diagnosis admits materially different fixes" in contract


def test_negative_security_or_consequential_decision_boundary_stops() -> None:
    contract = _recovery_contract()
    assert "security/product/architecture/authority" in contract
    assert "consequential human-decision boundaries" in contract


def test_non_implementation_role_cannot_gain_source_authority() -> None:
    contract = _recovery_contract()
    assert "this section never creates source mutation authority" in contract
    assert "a non-Implementation role may not use recovery as a route into source Implementation" in contract


def test_recovery_attempt_memory_reuses_existing_durable_state() -> None:
    contract = _recovery_contract()
    assert "Persist only the attempt/failure information actually required" in contract
    assert "using existing task/PR/controller/local durable state" in contract
    assert "Never add a retry database or alternate lifecycle authority" in contract
