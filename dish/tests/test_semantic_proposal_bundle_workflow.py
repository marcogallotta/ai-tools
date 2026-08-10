import json

import pytest
import dish_tool.admin as admin_module
import dish_tool.step8 as step8_module
from dish_tool.admin import DishAdminApplication
from dish_tool.database import content_identity
from dish_tool.errors import DishRuleError
from dish_tool.semantic_proposals import approve_semantic_proposal, queue_semantic_proposal
from tests.support.verification import TASK, make_app, review_and_inspect
from tests.support.semantic_proposal_bundle_workflow import (
    _approved_service_proposal_runtime,
    _case_test_service_fresh_invocation_claims_approved_bundle_without_old_run_identity,
)


def _approve_only(app, backend, proposal_id: str, reason: str = "Marco approves exact bundle."):
    """Low-level fixture helper for tests that exercise apply-proposal directly."""
    return approve_semantic_proposal(
        app.conn, proposal_id=proposal_id, live_title=backend.title,
        live_notes=backend.notes, reason=reason,
    )



@pytest.mark.smoke
def test_governed_large_correction_queues_one_bundle_and_fresh_run_applies_it(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion"))

    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large",
        reason="Apply Marco's settled whole-scallion default everywhere it governs.",
        file_path=str(candidate), run_id="proposal-author",
    )
    assert queued["code"] == "VALIDATION_FAILED"
    assert queued["errors"][0]["rule"] == "semantic_proposal_queued"
    assert queued["data"]["proposal_queued"] is True
    assert queued["data"]["batch_may_continue"] is True
    # Pending review must not leak the candidate into the canonical Asana task.
    assert "Use whole scallion" not in backend.notes
    proposal_id = queued["data"]["proposal_id"]

    proposal = app.conn.execute(
        "SELECT * FROM semantic_proposals WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    assert proposal["status"] == "pending"
    assert proposal["proposer_run_id"] == "proposal-author"
    assert app.conn.execute(
        "SELECT COUNT(*) FROM semantic_proposal_changes WHERE proposal_id=?", (proposal_id,)
    ).fetchone()[0] == 1

    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=app.release_loader
    )
    queue = admin.execute("review-queue", status="pending")
    assert queue["ok"] and queue["data"]["count"] == 1
    assert "candidate_notes" not in queue["data"]["proposals"][0]
    assert "protocol_text" not in queue["data"]["proposals"][0]
    inspected = admin.execute("review-inspect", proposal_id=proposal_id)
    assert inspected["ok"]
    assert inspected["data"]["proposal"]["changes"][0]["field"] == "Locks"
    assert "candidate_notes" in inspected["data"]["proposal"]
    assert "protocol_text" not in inspected["data"]["proposal"]

    operation_inspect = admin.execute("inspect", submission_id=operation_id)
    assert operation_inspect["ok"]
    assert operation_inspect["data"]["semantic_proposal"] == {
        "proposal_id": proposal_id,
        "status": "pending",
        "candidate_identity": proposal["candidate_identity"],
        "claimed_agent": None,
        "claimed_run_id": None,
    }
    assert operation_inspect["data"]["human_actions"][0]["command"] == "review-inspect"
    blocked_abandonment = admin.execute(
        "abandon-operation", submission_id=operation_id,
        reason="the proposer run ended",
    )
    assert blocked_abandonment["code"] == "WRONG_STATE"
    assert blocked_abandonment["errors"][0]["rule"] == "semantic_proposal_application_required"

    approved = admin.execute(
        "review-approve", proposal_id=proposal_id,
        reason="Marco approves the exact linked whole-scallion correction bundle.",
    )
    assert approved["ok"]
    assert approved["state"] == "applied"
    assert approved["data"]["proposal"]["status"] == "applied"
    assert approved["data"]["new_cycle_id"]
    assert "Locks: Keep crisp | Use whole scallion" in backend.notes

    # Marco approval and application remain separate durable events, but the second
    # action is performed mechanically by Dish rather than delegated to another AI.
    proposal_after = app.conn.execute(
        "SELECT claimed_agent,claimed_run_id,status FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert proposal_after["claimed_agent"] == "dish"
    assert proposal_after["claimed_run_id"]
    assert proposal_after["status"] == "applied"
    audit_types = [
        row[0] for row in app.conn.execute(
            "SELECT event_type FROM audit_events WHERE operation_id=? ORDER BY created_at,rowid",
            (operation_id,),
        )
    ]
    assert "semantic_proposal.approved" in audit_types
    assert "semantic_proposal.applied" in audit_types
    assert "semantic_proposal.application_completed" in audit_types
    claimable = app.execute("proposals", agent="gpt")
    assert claimable["data"]["count"] == 0
    assert app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=? AND consumed_at IS NULL",
        (operation_id,),
    ).fetchone()[0] == 0

    cycles = app.conn.execute(
        "SELECT cycle_number,outcome,completed_at FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number",
        (operation_id,),
    ).fetchall()
    assert len(cycles) == 2
    assert cycles[0]["outcome"] == "rejected" and cycles[0]["completed_at"]
    assert cycles[1]["outcome"] is None and cycles[1]["completed_at"] is None
    fact = app.conn.execute(
        "SELECT agent,run_id FROM operation_actor_facts WHERE operation_id=? AND role='material_editor' ORDER BY created_at DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    assert tuple(fact) == ("codex", "proposal-author")



def test_attributed_decision_append_does_not_require_formal_authorization(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "decisions-proposal.txt"
    candidate.write_text(
        TASK.replace(
            "### Research basis",
            "### Decisions\nHuman — Marco: Use chicken.\n### Research basis",
        )
    )

    preflight = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large",
        reason="Record Marco's settled chicken decision.",
        file_path=str(candidate), run_id="proposal-author",
    )
    assert preflight["code"] == "CONFIRMATION_REQUIRED"
    assert preflight["errors"][0]["rule"] == "decision_attestation_required"

    result = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large",
        reason="Record Marco's settled chicken decision.",
        file_path=str(candidate), run_id="proposal-author",
        governed_change_fields=["Decisions"],
    )
    assert result["ok"]
    assert "Human — Marco: Use chicken." in backend.notes
    assert app.conn.execute(
        "SELECT COUNT(*) FROM semantic_proposals WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 0
    assert app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 0
    attestation = app.conn.execute(
        """SELECT actor_agent,actor_provenance,details
             FROM audit_events
            WHERE operation_id=? AND event_type='decision.agent_attested'
            ORDER BY created_at DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    import json
    provenance = json.loads(attestation["actor_provenance"])
    details = json.loads(attestation["details"])
    assert attestation["actor_agent"] == "codex"
    assert provenance["run_id"] == "proposal-author"
    assert provenance["source"] == "agent-attested-conversation"
    assert details["formal_marco_authorization"] is False


def test_multiple_attributed_decision_appends_need_no_admin_ceremony(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "decisions-multi-proposal.txt"
    candidate.write_text(
        TASK.replace(
            "### Research basis",
            "### Decisions\n"
            "Human — Marco: Use chicken.\n"
            "Human — Marco: Use ginger.\n"
            "### Research basis",
        )
    )

    result = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large",
        reason="Record Marco's settled chicken and ginger decisions.",
        file_path=str(candidate), run_id="proposal-author",
        governed_change_fields=["Decisions"],
    )
    assert result["ok"]
    assert "Human — Marco: Use chicken." in backend.notes
    assert "Human — Marco: Use ginger." in backend.notes
    assert app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 0


def test_agent_attested_decision_plus_governed_lock_authorizes_only_lock(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "decision-and-lock.txt"
    candidate.write_text(
        TASK.replace(
            "Locks: Keep crisp",
            "Locks: Keep crisp | Use whole scallion",
        ).replace(
            "### Research basis",
            "### Decisions\nHuman — Marco: Use whole scallion.\n### Research basis",
        )
    )

    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large",
        reason="Record Marco's stated choice and apply the governed Lock consequence.",
        file_path=str(candidate), run_id="proposal-author",
        governed_change_fields=["Decisions", "Locks"],
    )
    assert queued["code"] == "VALIDATION_FAILED"
    assert queued["errors"][0]["rule"] == "semantic_proposal_queued"
    proposal_id = queued["data"]["proposal_id"]
    stored = app.conn.execute(
        "SELECT agent_attested_decisions_json FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    import json
    assert json.loads(stored["agent_attested_decisions_json"]) == ["Human — Marco: Use whole scallion."]

    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    approved = admin.execute(
        "review-approve", proposal_id=proposal_id,
        reason="Marco approves the exact governed Lock change.",
    )
    assert approved["ok"]
    assert approved["state"] == "applied"
    assert "Human — Marco: Use whole scallion." in backend.notes
    assert "Locks: Keep crisp | Use whole scallion" in backend.notes

    authorizations = app.conn.execute(
        "SELECT field_name FROM marco_authorizations WHERE operation_id=? ORDER BY field_name",
        (operation_id,),
    ).fetchall()
    assert [row["field_name"] for row in authorizations] == ["Locks"]

    approval_audit = app.conn.execute(
        """SELECT details FROM audit_events
             WHERE operation_id=? AND event_type='semantic_proposal.approved'
             ORDER BY created_at DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    import json
    details = json.loads(approval_audit["details"])
    assert details["authorization_fields"] == ["Locks"]
    assert details["agent_attested_decisions"] == ["Human — Marco: Use whole scallion."]

    attested = app.conn.execute(
        """SELECT actor_agent,actor_provenance FROM audit_events
             WHERE operation_id=? AND event_type='decision.agent_attested'
             ORDER BY created_at DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    provenance = json.loads(attested["actor_provenance"])
    assert attested["actor_agent"] == "codex"
    assert provenance["run_id"] == "proposal-author"
    assert provenance["source"] == "agent-attested-conversation"


def test_rewriting_existing_decision_still_requires_formal_authorization(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    # First append a remembered choice without formal authorization.
    first = tmp_path / "decision-first.txt"
    first.write_text(
        TASK.replace(
            "### Research basis",
            "### Decisions\nHuman — Marco: Use chicken.\n### Research basis",
        )
    )
    assert app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Record Marco choice.",
        file_path=str(first), run_id="proposal-author",
        governed_change_fields=["Decisions"],
    )["ok"]

    # A new verifier may not rewrite that durable choice as though it were another
    # ordinary remembered answer. Existing Decisions remain protected history.
    fresh = review_and_inspect(app, agent="gpt", run_id="second-verifier")
    rewritten = tmp_path / "decision-rewrite.txt"
    rewritten.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Human — Marco: Use chicken.", "Human — Marco: Use beef."
        )
    )
    queued = app.execute(
        "reject", agent="gpt", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Rewrite prior Marco choice.",
        file_path=str(rewritten), run_id="second-verifier",
    )
    assert queued["code"] == "VALIDATION_FAILED"
    assert queued["errors"][0]["rule"] == "semantic_proposal_queued"
    assert queued["data"]["proposal_queued"] is True


def test_malformed_governed_evidence_is_rejected_before_human_review(tmp_path):
    app, backend, operation_id, protocol_text = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL",
        (operation_id,),
    ).fetchone()
    assert cycle is not None

    candidate_text = TASK.replace(
        "Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion"
    )
    lines = candidate_text.splitlines()
    candidate_title = lines[0]
    candidate_notes = "\n".join(lines[1:]) + "\n"
    baseline = content_identity(backend.title, backend.notes)
    candidate = content_identity(candidate_title, candidate_notes)

    with pytest.raises(DishRuleError) as exc:
        queue_semantic_proposal(
            app.conn,
            task_gid="t",
            operation_id=operation_id,
            cycle_id=cycle["cycle_id"],
            baseline_identity=baseline.digest,
            candidate_identity=candidate.digest,
            candidate_title=candidate_title,
            candidate_notes=candidate_notes,
            proposal_reason="Malformed evidence must never reach Marco.",
            explanation={"problem": "test malformed evidence"},
            linked_changes=(
                {
                    "path": "planning.Locks",
                    "before": "Keep crisp",
                    "after": "WRONG EVIDENCE",
                },
            ),
            changes=(
                {
                    "field": "Locks",
                    "before": "Keep crisp",
                    "after": "WRONG EVIDENCE",
                },
            ),
            protocol_release=cycle["protocol_release"],
            protocol_text=protocol_text,
            proposer_agent="codex",
            proposer_run_id="proposal-author",
        )
    assert exc.value.rule == "semantic_proposal_evidence_invalid"
    assert app.conn.execute(
        "SELECT COUNT(*) FROM semantic_proposals WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 0


def test_section_move_does_not_invalidate_semantic_proposal(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]

    # Operational placement is not semantic proposal content.
    backend.section = "vq"
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    applied = admin.execute(
        "review-approve", proposal_id=proposal_id, reason="Marco approves exact bundle."
    )
    assert applied["ok"]
    assert applied["data"]["proposal"]["status"] == "applied"
    assert backend.section == "vq"
    assert "Use whole scallion" in backend.notes


def test_apply_reconciles_exact_approved_candidate_when_it_is_already_live(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    assert _approve_only(app, backend, proposal_id)["status"] == "approved"

    proposal = app.conn.execute(
        "SELECT candidate_title,candidate_notes FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    writes_before = backend.writes
    # Simulate a prior confirmed/legacy path that exposed the exact approved candidate
    # before proposal finalization. Applying should settle, not demand another rewrite/review.
    backend.title = proposal["candidate_title"]
    backend.notes = proposal["candidate_notes"]

    recoverable = admin.execute("inspect", submission_id=operation_id)
    assert recoverable["data"]["authoritative_view"]["legal_actions"] == ["apply-proposal"]
    assert recoverable["data"]["agent_actions_now"] == []
    assert recoverable["data"]["human_actions"][0]["command"] == "review-approve"
    assert recoverable["data"]["human_actions"][0]["arguments"]["positional"] == [proposal_id]

    applied = app.execute(
        "apply-proposal", proposal_id=proposal_id, agent="gpt",
        model="gpt-5.6-sol", run_id="fresh-applicant",
    )
    assert applied["ok"]
    assert applied["data"]["candidate_already_live"] is True
    assert applied["data"]["proposal"]["status"] == "applied"
    assert backend.writes == writes_before
    authorization = app.conn.execute(
        "SELECT consumed_at,consumed_identity FROM marco_authorizations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert authorization["consumed_at"]
    assert authorization["consumed_identity"] == applied["data"]["applied_identity"]


def test_admin_mechanical_application_failure_preserves_durable_approval(tmp_path, monkeypatch):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal-mechanical-failure.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    original_notes = backend.notes

    def fail_mechanical_application(*args, **kwargs):
        raise DishRuleError(
            "CONFLICT",
            "forced safe mechanical application failure",
            rule="forced_mechanical_application_failure",
        )

    monkeypatch.setattr(admin_module, "apply_semantic_proposal", fail_mechanical_application)
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    failed = admin.execute(
        "review-approve", proposal_id=proposal_id,
        reason="Marco approves this exact linked bundle.",
    )
    assert failed["code"] == "CONFLICT"
    error = failed["errors"][0]
    assert error["rule"] == "forced_mechanical_application_failure"
    assert error["approval_persisted"] is True
    assert error["proposal_status"] == "approved"
    assert backend.notes == original_notes
    proposal = app.conn.execute(
        "SELECT status,review_reason,claimed_agent FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert proposal["status"] == "approved"
    assert proposal["review_reason"] == "Marco approves this exact linked bundle."
    assert proposal["claimed_agent"] is None
    assert app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=? AND consumed_at IS NULL",
        (operation_id,),
    ).fetchone()[0] == 1

    # Restore the real function explicitly; the retry must reuse approval rather than create another one.
    from dish_tool.step8 import apply_semantic_proposal as real_apply_semantic_proposal
    monkeypatch.setattr(admin_module, "apply_semantic_proposal", real_apply_semantic_proposal)
    retried = admin.execute("review-approve", proposal_id=proposal_id)
    assert retried["ok"]
    assert retried["state"] == "applied"
    assert "Use whole scallion" in backend.notes
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='semantic_proposal.approved' AND operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 1



def _fail_after_confirmed_semantic_proposal_write(monkeypatch, proposal_id: str):
    original_complete = step8_module.complete_operation_step
    failed = {"done": False}

    def fail_once(conn, operation_id, step_name):
        if step_name == f"semantic_proposal_write:{proposal_id}" and not failed["done"]:
            failed["done"] = True
            raise DishRuleError(
                "CONFLICT",
                "forced failure after confirmed semantic proposal write",
                rule="forced_post_write_proposal_finalize_failure",
            )
        return original_complete(conn, operation_id, step_name)

    monkeypatch.setattr(step8_module, "complete_operation_step", fail_once)
    return original_complete


def test_confirmed_mechanical_write_recovery_is_idempotent_and_needs_no_reapproval(
    tmp_path, monkeypatch
):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal-confirmed-write-recovery.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    writes_before = backend.writes
    original_complete = _fail_after_confirmed_semantic_proposal_write(monkeypatch, proposal_id)
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)

    failed = admin.execute(
        "review-approve", proposal_id=proposal_id,
        reason="Marco approves this exact linked bundle.",
    )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    assert failed["errors"][0]["rule"] == "operation_partial_write_failure"
    assert failed["errors"][0]["original_failure_rule"] == "forced_post_write_proposal_finalize_failure"
    assert failed["errors"][0]["approval_persisted"] is True
    assert failed["errors"][0]["proposal_status"] == "claimed"
    assert backend.writes == writes_before + 1
    assert "Use whole scallion" in backend.notes

    proposal = app.conn.execute(
        "SELECT * FROM semantic_proposals WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    assert proposal["status"] == "claimed"
    assert proposal["claimed_agent"] == "dish"
    assert proposal["claimed_run_id"]
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE operation_id=? AND event_type='semantic_proposal.approved'",
        (operation_id,),
    ).fetchone()[0] == 1
    authorizations = app.conn.execute(
        "SELECT * FROM marco_authorizations WHERE operation_id=? ORDER BY authorization_id",
        (operation_id,),
    ).fetchall()
    assert len(authorizations) == 1
    assert authorizations[0]["consumed_at"]
    assert authorizations[0]["consumed_identity"] == proposal["candidate_identity"]
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE operation_id=? AND event_type='marco.authorization'",
        (operation_id,),
    ).fetchone()[0] == 1

    attempt = app.conn.execute(
        """SELECT * FROM write_attempts
             WHERE operation_id=? AND outcome='confirmed' AND intended_identity=?""",
        (operation_id, proposal["candidate_identity"]),
    ).fetchone()
    context = json.loads(attempt["context_json"])
    assert context["authorization_ids"] == [authorizations[0]["authorization_id"]]
    assert context["semantic_proposal_application"] == {
        "proposal_id": proposal_id,
        "candidate_identity": proposal["candidate_identity"],
        "application_actor": "dish",
        "application_owner_id": "dish-mechanical",
        "application_run_id": proposal["claimed_run_id"],
    }
    assert app.conn.execute(
        "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == 1

    # A different actor/run cannot take over the claimed confirmed-write recovery.
    wrong_run = app.execute(
        "apply-proposal", proposal_id=proposal_id, agent="gpt",
        model="gpt-5.6-sol", run_id="different-application-run",
    )
    assert wrong_run["code"] == "BACKEND_UNCERTAIN"
    assert wrong_run["errors"][0]["rule"] == "operation_partial_write_failure"
    assert wrong_run["errors"][0]["original_failure_rule"] == "semantic_proposal_claimed"
    assert backend.writes == writes_before + 1

    # Nor can merely different live content be reconciled as this exact application.
    exact_candidate_notes = backend.notes
    backend.notes = backend.notes.replace("Use whole scallion", "Use half scallion")
    wrong_candidate = admin.execute("review-approve", proposal_id=proposal_id)
    assert wrong_candidate["code"] == "BACKEND_UNCERTAIN"
    assert wrong_candidate["errors"][0]["rule"] == "operation_partial_write_failure"
    assert wrong_candidate["errors"][0]["original_failure_rule"] == "semantic_proposal_stale"
    assert backend.writes == writes_before + 1
    backend.notes = exact_candidate_notes

    inspect = admin.execute("review-inspect", proposal_id=proposal_id)
    assert inspect["ok"]
    assert inspect["data"]["admin_action"]["command"] == "review-approve"
    operation_inspect = admin.execute("inspect", submission_id=operation_id)
    assert operation_inspect["data"]["administrative_blocker"] is True
    assert operation_inspect["data"]["human_actions"]

    monkeypatch.setattr(step8_module, "complete_operation_step", original_complete)
    retried = admin.execute("review-approve", proposal_id=proposal_id)
    assert retried["ok"]
    assert retried["state"] == "applied"
    assert retried["data"]["proposal"]["status"] == "applied"
    assert retried["data"]["recovered_confirmed_write"] is True
    assert backend.writes == writes_before + 1

    final = app.conn.execute(
        "SELECT * FROM semantic_proposals WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    assert final["status"] == "applied"
    assert final["applied_identity"] == proposal["candidate_identity"]
    assert app.conn.execute(
        "SELECT COUNT(*) FROM semantic_proposals WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == 1
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE operation_id=? AND event_type='semantic_proposal.approved'",
        (operation_id,),
    ).fetchone()[0] == 1
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE operation_id=? AND event_type='semantic_proposal.applied'",
        (operation_id,),
    ).fetchone()[0] == 1
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE operation_id=? AND event_type='semantic_proposal.application_completed'",
        (operation_id,),
    ).fetchone()[0] == 1
    assert app.conn.execute(
        """SELECT COUNT(*) FROM write_attempts
             WHERE operation_id=? AND outcome='confirmed' AND intended_identity=?""",
        (operation_id, proposal["candidate_identity"]),
    ).fetchone()[0] == 1
    final_authorizations = app.conn.execute(
        "SELECT * FROM marco_authorizations WHERE operation_id=?", (operation_id,)
    ).fetchall()
    assert len(final_authorizations) == 1
    assert final_authorizations[0]["authorization_id"] == authorizations[0]["authorization_id"]
    assert final_authorizations[0]["consumed_at"] == authorizations[0]["consumed_at"]
    cycles = app.conn.execute(
        "SELECT cycle_number,outcome,completed_at FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number",
        (operation_id,),
    ).fetchall()
    assert len(cycles) == 2
    assert cycles[0]["outcome"] == "rejected" and cycles[0]["completed_at"]
    assert cycles[1]["outcome"] is None and cycles[1]["completed_at"] is None


@pytest.mark.parametrize(
    ("tamper", "expected_rule"),
    [
        ("proposal", "semantic_proposal_application_recovery_write_invalid"),
        ("candidate", "semantic_proposal_application_recovery_binding_mismatch"),
        ("actor", "semantic_proposal_application_recovery_binding_mismatch"),
        ("run", "semantic_proposal_application_recovery_binding_mismatch"),
        ("authorization_set", "semantic_proposal_application_recovery_authorization_mismatch"),
    ],
)
def test_confirmed_application_recovery_rejects_mismatched_durable_binding(
    tmp_path, monkeypatch, tamper, expected_rule
):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / f"proposal-binding-{tamper}.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    writes_before = backend.writes
    original_complete = _fail_after_confirmed_semantic_proposal_write(monkeypatch, proposal_id)
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    failed = admin.execute(
        "review-approve", proposal_id=proposal_id,
        reason="Marco approves this exact linked bundle.",
    )
    assert failed["errors"][0]["rule"] == "operation_partial_write_failure"
    assert failed["errors"][0]["original_failure_rule"] == "forced_post_write_proposal_finalize_failure"
    monkeypatch.setattr(step8_module, "complete_operation_step", original_complete)

    attempt = app.conn.execute(
        """SELECT attempt_id,context_json FROM write_attempts
             WHERE operation_id=? AND outcome='confirmed'
               AND json_extract(context_json, '$.semantic_proposal_application.proposal_id')=?""",
        (operation_id, proposal_id),
    ).fetchone()
    context = json.loads(attempt["context_json"])
    binding = context["semantic_proposal_application"]
    if tamper == "proposal":
        binding["proposal_id"] = "different-proposal"
    elif tamper == "candidate":
        binding["candidate_identity"] = "0" * 64
    elif tamper == "actor":
        binding["application_actor"] = "gpt"
    elif tamper == "run":
        binding["application_run_id"] = "different-run"
    else:
        context["authorization_ids"] = ["different-authorization"]

    # Simulate corrupted durable evidence. Production triggers make this context immutable;
    # dropping them here lets the recovery validator prove it still fails closed if storage
    # evidence is inconsistent rather than trusting current task text.
    app.conn.execute("DROP TRIGGER write_attempt_intent_immutable_update")
    app.conn.execute("DROP TRIGGER write_attempt_confirmed_append_only_update")
    app.conn.execute(
        "UPDATE write_attempts SET context_json=? WHERE attempt_id=?",
        (json.dumps(context, sort_keys=True, separators=(",", ":")), attempt["attempt_id"]),
    )

    retried = admin.execute("review-approve", proposal_id=proposal_id)
    assert retried["code"] == "BACKEND_UNCERTAIN"
    assert retried["errors"][0]["rule"] == "operation_partial_write_failure"
    assert retried["errors"][0]["original_failure_rule"] == expected_rule
    assert backend.writes == writes_before + 1
    assert app.conn.execute(
        "SELECT status FROM semantic_proposals WHERE proposal_id=?", (proposal_id,)
    ).fetchone()[0] == "claimed"


def test_stale_proposal_reports_exact_content_paths_and_excludes_operational_metadata(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]

    backend.notes = backend.notes.replace(
        "Purpose: Compare texture", "Purpose: Compare changed texture"
    )
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    result = admin.execute(
        "review-approve", proposal_id=proposal_id, reason="Marco approves exact bundle."
    )
    assert result["code"] == "CONFLICT"
    error = result["errors"][0]
    assert error["rule"] == "semantic_proposal_stale"
    assert any(change["path"] == "planning.Purpose" for change in error["content_changes"])
    assert "due date" in error["metadata_note"].lower()
    assert "section" in error["metadata_note"].lower()


@pytest.mark.smoke
def test_pending_or_approved_proposal_parks_original_verification_actions(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion"))
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    assert queued["data"]["proposal_queued"] is True
    blocked = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True,
        run_id="proposal-author",
    )
    assert blocked["code"] == "WRONG_STATE"
    assert blocked["errors"][0]["rule"] == "semantic_proposal_application_required"


def test_approved_proposal_is_not_advertised_after_exact_content_staleness(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal-stale-after-approval.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    assert _approve_only(app, backend, proposal_id)["status"] == "approved"

    backend.notes = backend.notes.replace(
        "Purpose: Compare texture", "Purpose: Compare changed texture"
    )

    inspected = admin.execute("inspect", submission_id=operation_id)
    view = inspected["data"]["authoritative_view"]
    assert view["legal_actions"] == []
    assert inspected["data"]["agent_actions_now"] == []
    assert view["semantic_proposal"]["block"]["rule"] == "semantic_proposal_stale"
    assert any(
        change["path"] == "planning.Purpose"
        for change in view["semantic_proposal"]["block"]["details"]["content_changes"]
    )
    reviewed = admin.execute("review-inspect", proposal_id=proposal_id)
    assert "agent_action" not in reviewed["data"]
    assert (
        reviewed["data"]["authoritative_view"]["semantic_proposal"]["block"]["rule"]
        == "semantic_proposal_stale"
    )
    cancelled = admin.execute(
        "discard", submission_id=operation_id, reason="cancel stale proposal operation"
    )
    assert cancelled["code"] == "WRONG_STATE"
    assert cancelled["errors"][0]["required_action"] == "inspect"
    assert cancelled["errors"][0]["proposal_block"]["rule"] == "semantic_proposal_stale"

    claimable = app.execute("proposals", agent="gpt")
    assert claimable["allowed_actions"] == []
    assert claimable["data"]["count"] == 0

    parked = app.execute("submit", submission_id=operation_id)
    assert parked["code"] == "WRONG_STATE"
    assert parked["errors"][0]["rule"] == "semantic_proposal_application_required"
    assert parked["errors"][0]["required_action"] == "inspect"
    assert parked["errors"][0]["proposal_block"]["rule"] == "semantic_proposal_stale"

    blocked = app.execute(
        "apply-proposal", proposal_id=proposal_id, agent="gpt",
        model="gpt-5.6-sol", run_id="fresh-applicant",
    )
    assert blocked["code"] == "CONFLICT"
    assert blocked["errors"][0]["rule"] == "semantic_proposal_stale"
    assert any(
        change["path"] == "planning.Purpose"
        for change in blocked["errors"][0]["content_changes"]
    )


def test_approved_proposal_requires_open_verification_cycle_everywhere(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal-cycle-missing.txt"
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    )
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    assert _approve_only(app, backend, proposal_id)["status"] == "approved"

    app.conn.execute(
        "UPDATE verification_cycles SET completed_at='2026-08-07T00:00:00Z' "
        "WHERE operation_id=? AND completed_at IS NULL",
        (operation_id,),
    )
    app.conn.commit()

    inspected = admin.execute("inspect", submission_id=operation_id)
    view = inspected["data"]["authoritative_view"]
    assert view["legal_actions"] == []
    assert view["semantic_proposal"]["block"]["rule"] == "verification_cycle_missing"

    claimable = app.execute("proposals", agent="gpt")
    assert claimable["allowed_actions"] == []
    assert claimable["data"]["count"] == 0

    blocked = app.execute(
        "apply-proposal", proposal_id=proposal_id, agent="gpt",
        model="gpt-5.6-sol", run_id="fresh-applicant",
    )
    assert blocked["code"] == "WRONG_STATE"
    assert blocked["errors"][0]["rule"] == "verification_cycle_missing"


def test_rejected_proposal_creates_no_authorization_and_restarts_verification(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion"))
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    rejected = admin.execute(
        "review-reject", proposal_id=proposal_id,
        reason="Marco rejects this interpretation.",
    )
    assert rejected["ok"]
    assert rejected["allowed_actions"] == ["start"]
    assert rejected["data"]["proposal"]["status"] == "rejected"
    assert rejected["data"]["new_cycle_id"]
    assert app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == 0
    cycles = app.conn.execute(
        "SELECT cycle_number,outcome,completed_at FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number",
        (operation_id,),
    ).fetchall()
    assert len(cycles) == 2
    assert cycles[0]["outcome"] == "rejected" and cycles[0]["completed_at"]
    assert cycles[1]["outcome"] is None and cycles[1]["completed_at"] is None
    operation = app.conn.execute(
        "SELECT verifier_agent,run_id,independence_attestation FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert tuple(operation) == (None, None, None)

    restarted = app.execute(
        "start", agent="gpt", task_gid="t", kind="verification",
        run_id="fresh-after-rejection", independence_attestation="independent fresh run",
    )
    assert restarted["ok"]
    assert restarted["submission_id"] == operation_id

def test_exact_rejected_candidate_cannot_be_requeued_without_new_evidence(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion"))
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    assert admin.execute(
        "review-reject", proposal_id=proposal_id,
        reason="Marco rejects this interpretation.",
    )["ok"]

    review_and_inspect(app, agent="gpt", run_id="second-proposer")
    repeated = app.execute(
        "reject", agent="gpt", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Repeat the same rejected correction",
        file_path=str(candidate), run_id="second-proposer",
    )
    assert repeated["code"] == "CONFLICT"
    assert repeated["errors"][0]["rule"] == "semantic_proposal_previously_rejected"
    assert repeated["errors"][0]["rejection_reason"] == "Marco rejects this interpretation."

def test_one_operation_cannot_hold_two_active_semantic_proposals(tmp_path):
    app, _backend, operation_id, protocol_text = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion"))
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    first = app.conn.execute(
        "SELECT * FROM semantic_proposals WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    with pytest.raises(DishRuleError) as exc:
        queue_semantic_proposal(
            app.conn,
            task_gid="t",
            operation_id=operation_id,
            cycle_id=first["cycle_id"],
            baseline_identity=first["baseline_identity"],
            candidate_identity="f" * 64,
            candidate_title=first["candidate_title"],
            candidate_notes=first["candidate_notes"] + "\n",
            proposal_reason="A different competing correction.",
            explanation={"problem": "different"},
            linked_changes=({"path": "planning.Locks", "before": "a", "after": "b"},),
            changes=({"field": "Locks", "before": "Keep crisp", "after": "Different"},),
            protocol_release=first["protocol_release"],
            protocol_text=protocol_text,
            proposer_agent="codex",
            proposer_run_id="proposal-author",
        )
    assert exc.value.rule == "semantic_proposal_operation_parked"

@pytest.mark.smoke
def test_service_fresh_invocation_claims_approved_bundle_without_old_run_identity(tmp_path) -> None:
    proposal = (
        _case_test_service_fresh_invocation_claims_approved_bundle_without_old_run_identity(
            tmp_path
        )
    )

    assert proposal["status"] == "applied"
    assert proposal["proposer_run_id"] == "proposal-run"
    assert proposal["claimed_run_id"] != "fresh-applicant"
    assert proposal["claimed_run_id"] != proposal["proposer_run_id"]
    assert proposal["applied_identity"]


def test_proposal_application_legality_matches_service_admin_and_execution(tmp_path):
    import uuid

    from dish_service.leases import ServicePrincipal

    service, _backend, proposal_id, task_gid = _approved_service_proposal_runtime(tmp_path)
    admin = service.execute_admin(
        "review-inspect",
        {"proposal_id": proposal_id},
        principal=ServicePrincipal("marco", str(uuid.uuid4())),
        request_id=str(uuid.uuid4()),
    )
    applicant = ServicePrincipal("applicant", str(uuid.uuid4()))
    exposed = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": task_gid,
            "kind": "verification",
            "independence_attestation": "independent",
        },
        principal=applicant,
        request_id=str(uuid.uuid4()),
    )

    assert admin["data"]["authoritative_view"]["legal_actions"] == ["apply-proposal"]
    assert exposed["data"]["authoritative_view"]["legal_actions"] == ["apply-proposal"]
    assert exposed["allowed_actions"] == ["apply-proposal"]
    assert exposed["data"]["agent_action"] == {
        "command": "apply-proposal", "arguments": {"proposal_id": proposal_id}
    }

    applied = service.execute_agent(
        "apply-proposal",
        {"proposal_id": proposal_id, "agent": "gpt", "model": "gpt-5.6-sol"},
        principal=applicant,
        request_id=str(uuid.uuid4()),
    )
    assert applied["ok"] is True
    assert applied["data"]["proposal"]["status"] == "applied"


def test_post_write_application_failure_keeps_proposal_claimed_for_recovery(tmp_path, monkeypatch):
    import dish_tool.step8 as step8_module
    from dish_tool.errors import DishRuleError

    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion"))
    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large", reason="Linked governed correction",
        file_path=str(candidate), run_id="proposal-author",
    )
    proposal_id = queued["data"]["proposal_id"]
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    assert _approve_only(app, backend, proposal_id)["status"] == "approved"

    def fail_after_write(*args, **kwargs):
        raise DishRuleError(
            "CONFLICT", "forced post-write finalization failure",
            rule="forced_post_write_failure",
        )

    monkeypatch.setattr(step8_module, "mark_semantic_proposal_applied", fail_after_write)
    result = app.execute(
        "apply-proposal", proposal_id=proposal_id, agent="gpt",
        model="gpt-5.6-sol", run_id="fresh-applicant",
    )
    assert result["code"] == "BACKEND_UNCERTAIN"
    row = app.conn.execute(
        "SELECT status,claimed_run_id FROM semantic_proposals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert tuple(row) == ("claimed", "fresh-applicant")
    assert "Use whole scallion" in backend.notes

@pytest.mark.smoke
def test_service_rejects_bundle_and_exposes_fresh_verification_round(tmp_path):
    from dish_service.leases import ServicePrincipal
    from tests.support.service_leases import _service

    backend = __import__("tests.support.verification", fromlist=["Backend"]).Backend()
    service = _service(tmp_path, backend)
    constructor = ServicePrincipal(owner_id="constructor", run_id="constructor-run")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
    )
    assert service.execute_agent(
        "prepare", {
            "agent": "gpt", "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"], "file_text": TASK,
        }, principal=constructor,
    )["ok"]
    proposer = ServicePrincipal(owner_id="proposer", run_id="proposal-run")
    assert service.execute_agent(
        "start", {
            "agent": "codex", "task_gid": "t", "kind": "verification",
            "independence_attestation": "independent",
        }, principal=proposer,
    )["ok"]
    assert service.execute_agent(
        "inspect", {"agent": "codex", "submission_id": started["submission_id"]},
        principal=proposer,
    )["ok"]
    queued = service.execute_agent(
        "reject", {
            "agent": "codex", "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"], "route": "large",
            "reason": "Apply the settled whole-scallion default.",
            "file_text": TASK.replace(
                "Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion"
            ),
        }, principal=proposer,
    )
    proposal_id = queued["data"]["proposal_id"]
    queue = service.execute_admin(
        "review-queue", {}, principal=ServicePrincipal(owner_id="marco-preview", run_id="marco-preview-run")
    )
    summary = queue["data"]["review_items"][0]["review_summary"]
    assert summary["outcome"] == "needs Marco review"
    assert summary["governed_changes"] == ["Locks"]
    assert summary["simplest_next_step"] == "Approve or reject this exact stored change bundle."

    rejected = service.execute_admin(
        "review-reject", {
            "proposal_id": proposal_id,
            "reason": "Marco rejects this interpretation.",
        }, principal=ServicePrincipal(owner_id="marco", run_id="marco-review"),
    )
    assert rejected["ok"]
    assert rejected["allowed_actions"] == ["start"]
    assert rejected["data"]["new_cycle_id"]

    fresh = service.execute_agent(
        "start", {
            "agent": "gpt", "task_gid": "t", "kind": "verification",
            "independence_attestation": "fresh after rejected proposal",
        }, principal=ServicePrincipal(owner_id="fresh", run_id="fresh-run"),
    )
    assert fresh["ok"]
    assert fresh["submission_id"] == started["submission_id"]
    assert fresh["data"]["service_access"]["state"] == "owned"


@pytest.mark.smoke
def test_action_apply_proposal_exact_replay_does_not_apply_bundle_twice(tmp_path):
    import uuid

    from dish_service.client import DishActionClient
    from dish_service.http import build_server
    from dish_tool.database_initialization import initialize_database
    from tests.support.thread_teardown import start_server_thread, stop_server

    service, backend, proposal_id, task_gid = _approved_service_proposal_runtime(tmp_path)
    server = build_server(service)
    thread = start_server_thread(server, daemon=True, name="apply-proposal-replay")
    host, port = server.server_address
    client = DishActionClient(
        f"http://{host}:{port}", token="action-secret", run_id=str(uuid.uuid4())
    )
    try:
        available = client.execute(
            "start",
            {
                "agent": "gpt",
                "task_gid": task_gid,
                "kind": "verification",
                "independence_attestation": "independent",
            },
        )
        assert available["allowed_actions"] == ["apply-proposal"]
        request_id = str(uuid.uuid4())
        arguments = {
            "proposal_id": proposal_id,
            "agent": "gpt",
            "model": "gpt-5.6-sol",
        }
        first = client.execute("apply-proposal", arguments, request_id=request_id)
        writes_after_first = backend.writes
        replayed = client.execute("apply-proposal", arguments, request_id=request_id)
        mismatch = client.execute(
            "apply-proposal",
            {**arguments, "model": "different-model"},
            request_id=request_id,
        )
    finally:
        stop_server(server, thread)

    assert first["ok"]
    assert first["data"]["request_id"] == request_id
    assert first["data"]["proposal"]["status"] == "applied"
    assert replayed["ok"]
    assert replayed["data"]["request_replayed"] is True
    assert replayed["data"]["request_id"] == request_id
    assert replayed["data"]["proposal"] == first["data"]["proposal"]
    assert backend.writes == writes_after_first
    assert mismatch["code"] == "CONFLICT"
    assert mismatch["errors"][0]["rule"] == "service_request_identity_conflict"

    conn = initialize_database(service.config.db_path)
    try:
        proposal = conn.execute(
            "SELECT status,claimed_run_id,applied_identity,operation_id "
            "FROM semantic_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        assert proposal["status"] == "applied"
        assert proposal["claimed_run_id"] == client.run_id
        assert proposal["applied_identity"]
        assert conn.execute(
            "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?",
            (proposal["operation_id"],),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM service_requests "
            "WHERE request_id=? AND command='apply-proposal' AND status='completed'",
            (request_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()
