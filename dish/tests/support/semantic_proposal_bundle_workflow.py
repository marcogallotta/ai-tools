import pytest
from dish_tool.admin import DishAdminApplication
from dish_tool.errors import DishRuleError
from dish_tool.semantic_proposals import approve_semantic_proposal, queue_semantic_proposal
from tests.support.verification import TASK, make_app, review_and_inspect




def _case_test_service_fresh_invocation_claims_approved_bundle_without_old_run_identity(tmp_path):
    from dish_service.leases import ServicePrincipal
    from dish_tool.database_initialization import initialize_database
    from tests.support.service_leases import _service

    backend = __import__("tests.support.verification", fromlist=["Backend"]).Backend()
    service = _service(tmp_path, backend)
    constructor = ServicePrincipal(owner_id="constructor", run_id="constructor-run")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
    )
    prepared = service.execute_agent(
        "prepare", {
            "agent": "gpt", "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"], "file_text": TASK,
        }, principal=constructor,
    )
    assert prepared["ok"]
    proposer = ServicePrincipal(owner_id="proposer", run_id="proposal-run")
    reviewed = service.execute_agent(
        "start", {
            "agent": "codex", "task_gid": "t", "kind": "verification",
            "independence_attestation": "independent",
        }, principal=proposer,
    )
    assert reviewed["ok"]
    inspected = service.execute_agent(
        "inspect", {"agent": "codex", "submission_id": started["submission_id"]},
        principal=proposer,
    )
    assert inspected["ok"]
    candidate = TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion")
    queued = service.execute_agent(
        "reject", {
            "agent": "codex", "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"], "route": "large",
            "reason": "Apply the settled whole-scallion default.",
            "file_text": candidate,
        }, principal=proposer,
    )
    assert queued["data"]["proposal_queued"] is True
    proposal_id = queued["data"]["proposal_id"]

    parked = service.execute_agent(
        "start", {
            "agent": "gpt", "task_gid": "t", "kind": "verification",
            "independence_attestation": "independent",
        }, principal=ServicePrincipal(owner_id="observer", run_id="observer-run"),
    )
    assert parked["allowed_actions"] == []
    assert parked["data"]["service_access"]["state"] == "awaiting_semantic_proposal_review"
    assert parked["data"]["required_admin_action"] == "review-inspect"

    admin = ServicePrincipal(owner_id="marco", run_id="marco-review")
    approved = service.execute_admin(
        "review-approve", {
            "proposal_id": proposal_id,
            "reason": "Marco approves this exact linked correction bundle.",
        }, principal=admin,
    )
    assert approved["ok"]

    # Normal Marco approval now performs the separate application action
    # mechanically, so the next AI sees the fresh Verification cycle directly.
    available = service.execute_agent(
        "start", {
            "agent": "gpt", "task_gid": "t", "kind": "verification",
            "independence_attestation": "independent",
        }, principal=ServicePrincipal(owner_id="applicant", run_id="fresh-applicant"),
    )
    assert available["ok"]
    assert available["allowed_actions"] == ["inspect"]
    assert "Use whole scallion" in backend.notes

    conn = initialize_database(service.config.db_path)
    try:
        proposal = conn.execute(
            "SELECT status,proposer_run_id,claimed_run_id,applied_identity FROM semantic_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        assert proposal["status"] == "applied"
        assert proposal["proposer_run_id"] == "proposal-run"
        assert proposal["claimed_run_id"] != "fresh-applicant"
        assert proposal["applied_identity"]
        result = dict(proposal)
    finally:
        conn.close()
    return result


def _approved_service_proposal_runtime(tmp_path):
    import uuid

    from dish_service.application import DishService
    from dish_service.config import ServiceConfig
    from dish_service.leases import ServicePrincipal
    from tests.support.service_foundation import _release_loader
    from tests.support.verification import Backend

    task_gid = "123456789"
    backend = Backend(task_gid=task_gid)
    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    constructor = ServicePrincipal("constructor", str(uuid.uuid4()))
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": task_gid, "kind": "initial"},
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )
    assert service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
        },
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )["ok"]
    proposer = ServicePrincipal("proposer", str(uuid.uuid4()))
    assert service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": task_gid,
            "kind": "verification",
            "independence_attestation": "independent",
        },
        principal=proposer,
        request_id=str(uuid.uuid4()),
    )["ok"]
    assert service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": started["submission_id"]},
        principal=proposer,
        request_id=str(uuid.uuid4()),
    )["ok"]
    queued = service.execute_agent(
        "reject",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "route": "large",
            "reason": "Apply the settled whole-scallion default.",
            "file_text": TASK.replace(
                "Locks: Keep crisp",
                "Locks: Keep crisp | Use whole scallion",
            ),
        },
        principal=proposer,
        request_id=str(uuid.uuid4()),
    )
    proposal_id = queued["data"]["proposal_id"]
    from dish_tool.database_initialization import initialize_database

    conn = initialize_database(service.config.db_path)
    try:
        approved = approve_semantic_proposal(
            conn,
            proposal_id=proposal_id,
            live_title=backend.title,
            live_notes=backend.notes,
            reason="Marco approves this exact linked correction bundle.",
        )
        assert approved["status"] == "approved"
    finally:
        conn.close()
    return service, backend, proposal_id, task_gid
