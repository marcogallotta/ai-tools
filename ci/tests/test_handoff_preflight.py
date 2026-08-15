from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from handoff_preflight import (  # noqa: E402
    HandoffReadiness,
    require_distinct_task_identities,
    validate_handoff,
)

TASK = "1217510503772980"
SHA = "a" * 40
TEXT = f"Audit handoff\nOwning task: {TASK}\nExact baseline: {SHA}\n"


def validate(**kwargs):
    values = dict(
        text=TEXT,
        required_role="Audit",
        destination_role="Audit",
        required_task_gid=TASK,
        task_readback_gid=TASK,
        required_baseline=SHA,
        baseline_readback=SHA,
    )
    values.update(kwargs)
    return validate_handoff(**values)


def test_audit_handoff_without_authorized_fresh_task_write_is_preparation_required():
    result = validate_handoff(
        text=f"Audit handoff\nOwning task: <AUDIT_TASK>\nExact baseline: {SHA}",
        required_role="Audit",
        destination_role="Audit",
        prerequisite_mutation="create a fresh Audit-round task",
        prerequisite_mutation_authorized=False,
    )
    # Unresolved handoff text remains invalid rather than being presented as copy-ready.
    assert result.readiness is HandoffReadiness.INVALID

    result = validate_handoff(
        text=f"Audit handoff\nExact baseline: {SHA}",
        required_role="Audit",
        destination_role="Audit",
        prerequisite_mutation="create a fresh Audit-round task",
        prerequisite_mutation_authorized=False,
    )
    assert result.readiness is HandoffReadiness.PREPARATION_REQUIRED
    assert result.next_action == "create a fresh Audit-round task"


def test_unresolved_placeholder_fails_even_when_other_identity_exists():
    result = validate(text=TEXT + "Round: <ROUND_ID>\n")
    assert result.readiness is HandoffReadiness.INVALID
    assert "unresolved handoff token" in result.reason


def test_known_implementation_destination_cannot_be_switched_to_audit_by_prose():
    result = validate(destination_role="Implementation")
    assert result.readiness is HandoffReadiness.ROUTING_REQUIRED
    assert result.next_action == "send only to a Audit Project/session"


def test_unknown_destination_is_routing_required_not_claimed_executable():
    result = validate(destination_role=None)
    assert result.readiness is HandoffReadiness.ROUTING_REQUIRED


def test_fresh_task_exact_sha_and_audit_destination_is_executable():
    assert validate().readiness is HandoffReadiness.EXECUTABLE


def test_durable_identity_must_be_in_text_and_readable():
    result = validate(required_identities={"PR": "109"})
    assert result.readiness is HandoffReadiness.INVALID
    result = validate(text=TEXT + "PR: 109\n", required_identities={"PR": None})
    assert result.readiness is HandoffReadiness.INVALID


def test_independent_audit_rounds_require_distinct_task_identities():
    require_distinct_task_identities(TASK, "1217510503772981")
    try:
        require_distinct_task_identities(TASK, TASK)
    except ValueError as exc:
        assert "distinct fresh task identities" in str(exc)
    else:
        raise AssertionError("expected duplicate task identity rejection")
