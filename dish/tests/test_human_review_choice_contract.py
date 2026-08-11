from __future__ import annotations

import json

import pytest

from dish_service.command_spec import action_openapi_argument_schema, validate_action_request
from dish_tool.admin import DishAdminApplication
from tests.support.verification import make_app, review_and_inspect


def _verification_review(app, *, options, run_id="choice-author", resume_status="pending-verification"):
    operation_id = app.conn.execute(
        "SELECT operation_id FROM operations ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]
    review_and_inspect(app, agent="codex", run_id=run_id)
    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="human-review",
        reason="This dish has about 25 g protein; a main dish normally needs 35 g.",
        resume_status=resume_status,
        run_id=run_id,
        human_review_confirmed=True,
        human_review_basis="How should this dish handle the protein shortfall?",
        repairs_considered="The verifier checked plausible recipe changes but the remaining choice belongs to Marco.",
        human_review_options=options,
        blocker_metric="protein",
        blocker_actual=25,
        blocker_limit=35,
        blocker_delta=-10,
        blocker_unit="g",
        blocker_basis="served edible portion",
    )
    assert held["ok"], held
    return operation_id


def _admin(app, backend):
    return DishAdminApplication(
        app.conn,
        backend=backend,
        release_loader=lambda: app._load_release("verification"),
        invocation_run_id="marco-choice-run",
    )


def test_human_review_requires_agent_authored_options_before_parking(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="missing-options")
    result = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="human-review",
        reason="Dish is below the protein target.",
        resume_status="pending-verification",
        run_id="missing-options",
        human_review_confirmed=True,
        human_review_basis="How should the protein shortfall be handled?",
        repairs_considered="Several plausible routes remain and Marco must choose one.",
    )
    assert result["code"] == "CONFIRMATION_REQUIRED"
    assert result["errors"][0]["rule"] == "human_review_preflight_required"
    retry = result["errors"][0]["retry"]
    assert retry["human_review_options"][0]["label"] == "<recommended route>"


def test_public_reject_schema_requires_options_for_human_review_and_accepts_nested_authorization():
    schema = action_openapi_argument_schema("reject")
    human = next(
        item for item in schema["oneOf"]
        if item["properties"]["route"]["const"] == "human-review"
    )
    assert "human_review_options" in human["required"]
    options = human["properties"]["human_review_options"]
    assert options["minItems"] == 1
    assert options["maxItems"] == 6
    assert options["items"]["required"] == ["label", "decision"]
    assert options["items"]["properties"]["authorization"]["required"] == [
        "field", "before", "after"
    ]

    # The server-side scalar validator must understand the same nested shape.
    client, arguments = validate_action_request(
        "reject",
        {
            "client": {
                "run_id": "11111111-1111-4111-8111-111111111111",
                "request_id": "22222222-2222-4222-8222-222222222222",
            },
            "arguments": {
                "submission_id": "33333333-3333-4333-8333-333333333333",
                "agent": "codex",
                "reason": "Dish is below the protein target.",
                "route": "human-review",
                "resume_status": "pending-verification",
                "human_review_confirmed": True,
                "human_review_basis": "How should the shortfall be handled?",
                "repairs_considered": "Two plausible routes remain.",
                "human_review_options": [
                    {
                        "label": "Approve an exemption",
                        "decision": "Approve a protein exemption.",
                        "authorization": {
                            "field": "Exemptions",
                            "before": "None",
                            "after": "Protein target exemption approved by Marco",
                        },
                    }
                ],
            },
        },
    )
    assert client["request_id"].startswith("2222")
    assert arguments["human_review_options"][0]["authorization"]["field"] == "Exemptions"


def test_review_queue_persists_ranked_choices_and_A_is_recommended(tmp_path):
    app, backend, _operation_id, _ = make_app(tmp_path)
    options = [
        {
            "label": "Increase the cheese enough to meet the target",
            "decision": "Increase the cheese enough to meet the main-dish protein target.",
        },
        {
            "label": "Approve a protein exemption",
            "decision": "Keep the dish as-is and approve a protein exemption.",
        },
    ]
    operation_id = _verification_review(app, options=options)
    admin = _admin(app, backend)

    queue = admin.execute("review-queue", status="pending")
    assert queue["ok"] and queue["data"]["count"] == 1
    item = queue["data"]["review_items"][0]
    assert item["operation_id"] == operation_id
    assert item["review_summary"]["issue"].startswith("This dish has about 25 g protein")
    assert [option["option_id"] for option in item["human_review_options"]] == ["A", "B"]
    assert item["human_review_options"][0]["recommended"] is True
    assert item["human_review_options"][1]["recommended"] is False
    assert item["human_review_options"][0]["label"] == options[0]["label"]

    inspected = admin.execute("review-inspect", proposal_id=item["review_id"])
    assert inspected["ok"]
    assert [action["command"] for action in inspected["data"]["human_actions"]] == [
        "review-approve"
    ]
    rendered = inspected["data"]["review_item"]
    assert rendered["human_review_options"][0]["decision"] == options[0]["decision"]


def test_selecting_A_records_exact_agent_decision(tmp_path):
    app, backend, _operation_id, _ = make_app(tmp_path)
    decision = "Increase the cheese enough to meet the main-dish protein target."
    operation_id = _verification_review(
        app,
        options=[
            {"label": "Increase the cheese", "decision": decision},
            {"label": "Approve an exemption", "decision": "Approve a protein exemption."},
        ],
    )
    admin = _admin(app, backend)
    item = admin.execute("review-queue", status="pending")["data"]["review_items"][0]

    resolved = admin.execute("review-approve", proposal_id=item["review_id"], choice="A")
    assert resolved["ok"], resolved
    assert resolved["data"]["selected_choice"] == "A"
    assert resolved["data"]["selected_decision"] == decision
    assert resolved["data"]["governed_authorization"] is None
    assert f"Human — Marco: human_review resolved — {decision}" in backend.notes
    assert app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == 0


def test_structured_choice_records_exact_unused_authorization_for_continuation(tmp_path):
    app, backend, _operation_id, _ = make_app(tmp_path)
    after = "Protein target exemption approved by Marco"
    operation_id = _verification_review(
        app,
        options=[
            {
                "label": "Approve a protein exemption",
                "decision": "Keep the dish as-is and approve a protein exemption.",
                "authorization": {"field": "Exemptions", "before": "None", "after": after},
            },
            {
                "label": "Increase the cheese",
                "decision": "Increase the cheese enough to meet the main-dish protein target.",
            },
        ],
    )
    admin = _admin(app, backend)
    item = admin.execute("review-queue", status="pending")["data"]["review_items"][0]

    resolved = admin.execute("review-approve", proposal_id=item["review_id"], choice="A")
    assert resolved["ok"], resolved
    auth = resolved["data"]["governed_authorization"]
    assert auth["field"] == "Exemptions"
    assert auth["before"] == "None"
    assert auth["after"] == after
    row = app.conn.execute(
        """SELECT field_name,before_json,after_json,consumed_at,actor_run_id
             FROM marco_authorizations WHERE operation_id=?""",
        (operation_id,),
    ).fetchone()
    assert row is not None
    assert row["field_name"] == "Exemptions"
    assert json.loads(row["before_json"]) == "None"
    assert json.loads(row["after_json"]) == after
    assert row["consumed_at"] is None
    assert row["actor_run_id"] == "marco-choice-run"
    assert "recorded its exact governed-field authorization" in resolved["data"]["effect"]
    assert "Exemptions: None" in backend.notes  # selection authorized; it did not silently edit the candidate


def test_other_records_free_text_without_inventing_authorization(tmp_path):
    app, backend, _operation_id, _ = make_app(tmp_path)
    operation_id = _verification_review(
        app,
        options=[{"label": "Increase the cheese", "decision": "Increase the cheese."}],
    )
    admin = _admin(app, backend)
    item = admin.execute("review-queue", status="pending")["data"]["review_items"][0]
    instruction = "Keep the current portion, add a bean side, and verify the combined serving."

    resolved = admin.execute(
        "review-approve",
        proposal_id=item["review_id"],
        choice="other",
        reason=instruction,
    )
    assert resolved["ok"], resolved
    assert resolved["data"]["selected_choice"] is None
    assert resolved["data"]["selected_decision"] == instruction
    assert resolved["data"]["governed_authorization"] is None
    assert instruction in backend.notes
    assert app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == 0


def test_preconstruction_human_review_uses_review_queue_and_rejects_unbound_authorization(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    # make_app starts with a constructed candidate; use a fresh initial operation on a bare task instead.
    from tests.support.service_leases import _service
    from dish_service.leases import ServicePrincipal

    pre_dir = tmp_path / "preconstruction"
    pre_dir.mkdir()
    service = _service(pre_dir, backend)
    researcher = ServicePrincipal(owner_id="researcher", run_id="researcher-run")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=researcher
    )
    op_id = started["submission_id"]

    rejected = service.execute_agent(
        "reject",
        {
            "agent": "gpt",
            "submission_id": op_id,
            "route": "human-review",
            "reason": "Marco needs to choose which serving format Research should build.",
            "resume_status": "pending-research",
            "human_review_confirmed": True,
            "human_review_basis": "Which serving format should Research build?",
            "repairs_considered": "Both formats are plausible and neither is settled by existing intent.",
            "human_review_options": [
                {"label": "Build the bowl format", "decision": "Build the bowl format."},
                {"label": "Build the plated format", "decision": "Build the plated format."},
            ],
        },
        principal=researcher,
    )
    assert rejected["ok"], rejected
    marco = ServicePrincipal(owner_id="marco", run_id="marco-preconstruction")
    queue = service.execute_admin("review-queue", {}, principal=marco)
    assert queue["ok"]
    item = next(row for row in queue["data"]["review_items"] if row["operation_id"] == op_id)
    assert item["review_id"] == op_id
    assert item["preconstruction"] is True
    assert item["human_review_options"][0]["recommended"] is True

    # A pre-construction option cannot claim an exact governed before/after value,
    # because no reviewed candidate exists yet to bind that authorization to.
    pre_auth_dir = tmp_path / "preconstruction-auth"
    pre_auth_dir.mkdir()
    service2 = _service(pre_auth_dir, backend)
    researcher2 = ServicePrincipal(owner_id="researcher", run_id="researcher-run-2")
    started2 = service2.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"}, principal=researcher2
    )
    invalid = service2.execute_agent(
        "reject",
        {
            "agent": "gpt",
            "submission_id": started2["submission_id"],
            "route": "human-review",
            "reason": "Marco needs to choose an exemption before construction.",
            "resume_status": "pending-research",
            "human_review_confirmed": True,
            "human_review_basis": "Should Research use an exemption?",
            "repairs_considered": "The candidate has not been constructed yet.",
            "human_review_options": [
                {
                    "label": "Approve an exemption",
                    "decision": "Approve an exemption.",
                    "authorization": {
                        "field": "Exemptions",
                        "before": "None",
                        "after": "Approved before construction",
                    },
                }
            ],
        },
        principal=researcher2,
    )
    assert invalid["code"] == "INVALID_ARGUMENT"
    assert invalid["errors"][0]["rule"] == "preconstruction_human_review_authorization_unavailable"
