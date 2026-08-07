import pytest
from dish_tool.admin import DishAdminApplication
from dish_tool.database import content_identity
from dish_tool.errors import DishRuleError
from dish_tool.semantic_proposals import queue_semantic_proposal
from tests.support.verification import TASK, make_app, review_and_inspect
from tests.support.semantic_proposal_bundle_workflow import (
    _approved_service_proposal_runtime,
    _case_test_service_fresh_invocation_claims_approved_bundle_without_old_run_identity,
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
    assert approved["data"]["proposal"]["status"] == "approved"
    assert "agent_action" not in approved["data"]
    reviewed = admin.execute("review-inspect", proposal_id=proposal_id)
    assert reviewed["data"]["authoritative_view"]["legal_actions"] == ["apply-proposal"]
    assert reviewed["data"]["agent_action"] == {
        "command": "apply-proposal", "arguments": {"proposal_id": proposal_id}
    }
    authoritative = admin.execute("inspect", submission_id=operation_id)
    assert authoritative["data"]["agent_actions_now"] == [{
        "command": "apply-proposal", "arguments": {"proposal_id": proposal_id}
    }]
    assert authoritative["data"]["authoritative_view"]["legal_actions"] == ["apply-proposal"]
    claimable = app.execute("proposals", agent="gpt")
    assert claimable["allowed_actions"] == ["apply-proposal"]
    assert [item["proposal_id"] for item in claimable["data"]["proposals"]] == [proposal_id]
    # Approval is authorization, not application.
    assert "Use whole scallion" not in backend.notes
    assert app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=? AND consumed_at IS NULL",
        (operation_id,),
    ).fetchone()[0] == 1

    applied = app.execute(
        "apply-proposal", proposal_id=proposal_id, agent="gpt",
        model="gpt-5.6-sol", run_id="fresh-applicant",
    )
    assert applied["ok"]
    assert applied["data"]["proposal"]["status"] == "applied"
    assert applied["data"]["new_cycle_id"]
    assert "Locks: Keep crisp | Use whole scallion" in backend.notes
    assert applied["allowed_actions"] == ["start"]

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



def test_decisions_governed_change_survives_json_round_trip_and_applies(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="proposal-author")
    candidate = tmp_path / "decisions-proposal.txt"
    candidate.write_text(
        TASK.replace(
            "### Research basis",
            "### Decisions\nHuman — Marco: Use chicken.\n### Research basis",
        )
    )

    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large",
        reason="Record Marco's settled chicken decision.",
        file_path=str(candidate), run_id="proposal-author",
    )
    assert queued["code"] == "VALIDATION_FAILED"
    assert queued["errors"][0]["rule"] == "semantic_proposal_queued"
    proposal_id = queued["data"]["proposal_id"]

    stored_change = app.conn.execute(
        "SELECT before_json,after_json FROM semantic_proposal_changes WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert stored_change is not None
    assert stored_change["before_json"] == "[]"
    assert "Use chicken" in stored_change["after_json"]

    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    approved = admin.execute(
        "review-approve", proposal_id=proposal_id,
        reason="Marco approves recording this exact decision.",
    )
    assert approved["ok"]

    applied = app.execute(
        "apply-proposal", proposal_id=proposal_id, agent="gpt",
        model="gpt-5.6-sol", run_id="fresh-applicant",
    )
    assert applied["ok"]
    assert applied["data"]["proposal"]["status"] == "applied"
    assert "Human — Marco: Use chicken." in backend.notes


def test_multi_entry_decisions_authorization_reserves_across_json_serializers(tmp_path):
    """A single-element Decisions array round-trips identically under either JSON
    serializer, so it cannot reveal a serializer mismatch between the authorization
    writer (proposal approval) and the authorization reader (apply-proposal
    reservation). A multi-element array does: ``json.dumps([...], sort_keys=True)``
    inserts a space after each comma that ``json.dumps([...], sort_keys=True,
    separators=(",", ":"))`` omits, so an authorization recorded with one
    serializer would silently fail the exact-string lookup performed with the
    other, leaving an approved proposal permanently unapplyable."""
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

    queued = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, route="large",
        reason="Record Marco's settled chicken and ginger decisions.",
        file_path=str(candidate), run_id="proposal-author",
    )
    assert queued["code"] == "VALIDATION_FAILED"
    assert queued["errors"][0]["rule"] == "semantic_proposal_queued"
    proposal_id = queued["data"]["proposal_id"]

    admin = DishAdminApplication(app.conn, backend=backend, release_loader=app.release_loader)
    approved = admin.execute(
        "review-approve", proposal_id=proposal_id,
        reason="Marco approves recording both exact decisions.",
    )
    assert approved["ok"]

    applied = app.execute(
        "apply-proposal", proposal_id=proposal_id, agent="gpt",
        model="gpt-5.6-sol", run_id="fresh-applicant",
    )
    assert applied["ok"]
    assert applied["data"]["proposal"]["status"] == "applied"
    assert "Human — Marco: Use chicken." in backend.notes
    assert "Human — Marco: Use ginger." in backend.notes


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
    assert admin.execute(
        "review-approve", proposal_id=proposal_id, reason="Marco approves exact bundle."
    )["ok"]
    applied = app.execute(
        "apply-proposal", proposal_id=proposal_id, agent="gpt",
        model="gpt-5.6-sol", run_id="fresh-applicant",
    )
    assert applied["ok"]
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
    assert admin.execute(
        "review-approve", proposal_id=proposal_id, reason="Marco approves exact bundle."
    )["ok"]

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
    assert recoverable["data"]["agent_actions_now"] == [{
        "command": "apply-proposal", "arguments": {"proposal_id": proposal_id}
    }]

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
    assert admin.execute(
        "review-approve", proposal_id=proposal_id, reason="Marco approves exact bundle."
    )["ok"]

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
    assert admin.execute(
        "review-approve", proposal_id=proposal_id, reason="Marco approves exact bundle."
    )["ok"]

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
    assert proposal["claimed_run_id"] == "fresh-applicant"
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
    assert admin.execute(
        "review-approve", proposal_id=proposal_id, reason="Marco approves exact bundle."
    )["ok"]

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
    from dish_tool.database import initialize_database
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
