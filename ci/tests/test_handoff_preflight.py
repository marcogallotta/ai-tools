from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from handoff_preflight import (  # noqa: E402
    HandoffHost,
    HandoffPresentationKind,
    HandoffReadiness,
    prepare_handoff_presentation,
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


def test_locator_first_handoff_is_one_copy_block_without_payload_replay():
    result = prepare_handoff_presentation(
        payload="",
        host=HandoffHost.CHATGPT,
        manual_relay_required=True,
        reconstructable_locator="Review PR #245.",
    )
    assert result.kind is HandoffPresentationKind.INLINE
    assert result.copy_block == "```\nReview PR #245.\n```"


def test_locator_preserves_non_reconstructable_delta_and_counts_combined_content():
    delta = "Constraint: use unpublished fixture bytes."
    inline = prepare_handoff_presentation(
        payload=delta,
        host=HandoffHost.CHATGPT,
        manual_relay_required=True,
        reconstructable_locator="Implement task 1217673638828680.",
    )
    assert inline.kind is HandoffPresentationKind.INLINE
    assert inline.copy_block == (
        "```\nImplement task 1217673638828680.\n"
        "Constraint: use unpublished fixture bytes.\n```"
    )

    combined_over_limit = prepare_handoff_presentation(
        payload="x" * 690,
        host=HandoffHost.CHATGPT,
        manual_relay_required=True,
        reconstructable_locator="Review PR #247.",
    )
    assert combined_over_limit.kind is HandoffPresentationKind.BLOCKED
    assert combined_over_limit.copy_block is None


def test_inline_threshold_requires_both_limits_and_ignores_blank_lines():
    exactly_eight_lines = "\n\n".join(["x" * 86] * 7 + ["x" * 84])
    assert len(exactly_eight_lines) == 700
    result = prepare_handoff_presentation(
        payload=exactly_eight_lines,
        host=HandoffHost.CHATGPT,
        manual_relay_required=True,
    )
    assert result.kind is HandoffPresentationKind.INLINE

    over_chars = prepare_handoff_presentation(
        payload=exactly_eight_lines + "x",
        host=HandoffHost.CHATGPT,
        manual_relay_required=True,
    )
    assert over_chars.kind is HandoffPresentationKind.BLOCKED

    over_lines = prepare_handoff_presentation(
        payload="\n".join(["x"] * 9),
        host=HandoffHost.CHATGPT,
        manual_relay_required=True,
    )
    assert over_lines.kind is HandoffPresentationKind.BLOCKED


def test_large_local_handoff_writes_exact_private_file_and_only_shows_path(tmp_path):
    payload = "non-reconstructable\n" * 9
    result = prepare_handoff_presentation(
        payload=payload,
        host=HandoffHost.LOCAL,
        manual_relay_required=True,
        reconstructable_locator="Implement task 1217673638828680.",
        temp_directory=tmp_path,
    )
    assert result.kind is HandoffPresentationKind.LOCAL_FILE
    assert result.file_path is not None and result.file_path.is_absolute()
    assert result.file_path.read_text() == f"Implement task 1217673638828680.\n{payload}"
    assert result.file_path.stat().st_mode & 0o777 == 0o600
    assert result.copy_block == f"```\n{result.file_path}\n```"
    assert payload not in result.copy_block


def test_large_chatgpt_handoff_uses_supported_artifact_or_reports_capability_blocker():
    payload = "non-reconstructable\n" * 9
    transferred = []
    attached = prepare_handoff_presentation(
        payload=payload,
        host=HandoffHost.CHATGPT,
        manual_relay_required=True,
        reconstructable_locator="Review PR #247.",
        chatgpt_artifact_writer=lambda content: transferred.append(content)
        or "Attached: complete-handoff.txt",
    )
    assert attached.kind is HandoffPresentationKind.CHATGPT_ARTIFACT
    assert attached.copy_block == "```\nAttached: complete-handoff.txt\n```"
    assert transferred == [f"Review PR #247.\n{payload}"]
    assert payload not in attached.copy_block

    blocked = prepare_handoff_presentation(
        payload=payload,
        host=HandoffHost.CHATGPT,
        manual_relay_required=True,
    )
    assert blocked.kind is HandoffPresentationKind.BLOCKED
    assert blocked.copy_block is None
    assert "supported artifact" in blocked.reason


def test_no_manual_relay_adds_no_presentation_ceremony():
    result = prepare_handoff_presentation(
        payload="unused",
        host=HandoffHost.LOCAL,
        manual_relay_required=False,
    )
    assert result.kind is HandoffPresentationKind.NONE
    assert result.copy_block is None
