from tests.test_dish_tool_step7_verification import make_app


def test_structural_pass_does_not_replace_verifier_semantic_attestation(tmp_path):
    app, _backend, operation_id, _candidate = make_app(tmp_path)
    review = app.execute(
        "start", agent="codex", task_gid="t", kind="verification", run_id="protocol-hook-review",
        independence_attestation="independent",
    )
    assert review["ok"]
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]

    result = app.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=False,
        provenance_complete=True,
        run_id="protocol-hook-review",
        independence_attestation="independent",
    )

    assert not result["ok"]
    assert result["code"] == "VALIDATION_FAILED"
    assert any(error["rule"] == "verification_inputs_incomplete" for error in result["errors"])
    assert result["allowed_actions"] == ["approve", "reject"]
