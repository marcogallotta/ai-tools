"""PostgreSQL Verification Human Review admits only the closed Marco-authority list."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_contract import postgres_action_argument_schema
from dish_pg.database import session_scope
from dish_pg.document_authority import parse_canonical_document
from dish_pg.human_review_preflight import HUMAN_REVIEW_CATEGORIES
from dish_service.action_guidance import connected_agent_guidance
from dish_service.command_spec import action_openapi_argument_schema
from tests.support.postgresql.command import _call, _verification_ready
from tests.support.postgresql.workflow import _next, _register_run, workflow_db  # noqa: F401

OPTIONS = [
    {"label": "Waive fat", "decision": "[nutrition-fat] approved by Marco"},
    {"label": "Trim the oil", "decision": "Reduce frying oil to 20 g and re-verify"},
]
BLOCKER = {
    "blocker_metric": "fat",
    "blocker_actual": 52,
    "blocker_limit": 40,
    "blocker_delta": 12,
    "blocker_unit": "g",
    "blocker_basis": "ingredient calculation",
}


def _reject(session, ids, context, task_id, **arguments):
    port, _author, verifier_run, started, _prepared, _inspection = _verification_ready(
        session, ids, context, task_id
    )
    result = port.execute(
        _call(
            "reject",
            run_id=verifier_run,
            request_id=_next(ids),
            owner="verifier-owner",
            principal="verification",
            arguments={
                "task_id": str(task_id),
                "operation_id": started.data["operation_id"],
                "agent": "codex",
                "route": "human-review",
                "reason": "The dish exceeds its fat target",
                **arguments,
            },
        )
    )
    operation = session.get(wf.WorkflowOperation, uuid.UUID(started.data["operation_id"]))
    open_reviews = session.scalar(
        select(func.count())
        .select_from(wf.HumanReviewRequirement)
        .where(wf.HumanReviewRequirement.operation_id == operation.operation_id)
    )
    return result, operation, open_reviews


@pytest.mark.parametrize(
    "category", [None, "", "verifier_uncertainty", "operational_loop"]
)
def test_human_review_outside_closed_list_is_redirected_to_chat(
    workflow_db, category  # noqa: F811
) -> None:
    factory, ids, context, task_id = workflow_db
    arguments = {"human_review_basis": "Is this authentic?", "human_review_options": OPTIONS}
    if category is not None:
        arguments["human_review_category"] = category
    with session_scope(factory) as session:
        result, operation, open_reviews = _reject(session, ids, context, task_id, **arguments)
        assert not result.ok
        assert result.code == "HUMAN_REVIEW_NOT_ALLOWED"
        assert result.data["allowed_categories"] == list(HUMAN_REVIEW_CATEGORIES)
        assert "Ask Marco this in chat and continue from his answer." in result.data["message"]
        assert "No hold was opened" in result.data["instruction"]
        assert open_reviews == 0
        assert operation.phase == "await_verification"
        guidance = connected_agent_guidance(
            {"command": "reject", "code": result.code, "data": dict(result.data)}
        )
        assert any("ask Marco the question" in line for line in guidance["instructions"])


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"human_review_options": OPTIONS}, "HUMAN_REVIEW_PREFLIGHT_REQUIRED"),
        ({"human_review_basis": "Waive fat?"}, "HUMAN_REVIEW_PREFLIGHT_REQUIRED"),
        (
            {"human_review_basis": "Waive fat?", "human_review_options": []},
            "HUMAN_REVIEW_PREFLIGHT_REQUIRED",
        ),
        (
            {"human_review_basis": "Waive fat?", "human_review_options": [{"label": "A"}]},
            "HUMAN_REVIEW_PREFLIGHT_REQUIRED",
        ),
        (
            {
                "human_review_basis": "Waive fat?",
                "human_review_options": [
                    {
                        "label": "Waive",
                        "decision": "waive",
                        "authorization": {
                            "field": "Exemptions",
                            "before": "",
                            "after": "[nutrition-fat]",
                        },
                    }
                ],
            },
            "HUMAN_REVIEW_OPTION_AUTHORIZATION_UNSUPPORTED",
        ),
        (
            {"human_review_basis": "Waive fat?", "human_review_options": OPTIONS},
            "QUANTIFIED_BLOCKER_REQUIRED",
        ),
        (
            {
                "human_review_basis": "Waive fat?",
                "human_review_options": OPTIONS,
                "blocker_metric": "fat",
                "blocker_actual": 52,
            },
            "QUANTIFIED_BLOCKER_INCOMPLETE",
        ),
        (
            {
                "human_review_basis": "Waive fat?",
                "human_review_options": OPTIONS,
                **BLOCKER,
                "blocker_delta": 10,
            },
            "QUANTIFIED_BLOCKER_DELTA_MISMATCH",
        ),
        (
            {
                "human_review_basis": "Waive fat?",
                "human_review_options": OPTIONS,
                **BLOCKER,
                "resume_status": "pending-research",
            },
            "INVALID_RESUME_STATUS",
        ),
    ],
)
def test_human_review_requires_decision_ready_packet(
    workflow_db, arguments, code  # noqa: F811
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        result, operation, open_reviews = _reject(
            session,
            ids,
            context,
            task_id,
            human_review_category="nutrition_exemption",
            **arguments,
        )
        assert not result.ok
        assert result.code == code
        assert open_reviews == 0
        assert operation.phase == "await_verification"


def test_admitted_human_review_records_decision_packet(workflow_db) -> None:  # noqa: F811
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        result, operation, open_reviews = _reject(
            session,
            ids,
            context,
            task_id,
            human_review_category="nutrition_exemption",
            human_review_basis="Waive the fat target for this dish?",
            human_review_options=OPTIONS,
            repairs_considered="Leaner cut changes the dish's Purpose",
            resume_status="pending-verification",
            **BLOCKER,
        )
        assert result.ok, result.data
        assert open_reviews == 1
        assert operation.phase == "held_human"
        assert operation.persisted_actions == ["record-human-decision"]
        assert result.data["human_review_category"] == "nutrition_exemption"
        assert [option["option_id"] for option in result.data["human_review_options"]] == ["A", "B"]
        assert result.data["human_review_options"][0]["recommended"] is True
        assert result.data["quantified_blocker"]["delta"] == 12
        requirement = session.get(
            wf.HumanReviewRequirement, uuid.UUID(result.data["requirement_id"])
        )
        question = requirement.question
        assert question.startswith("The dish exceeds its fat target")
        assert "Category: a nutrition exemption" in question
        assert "Decision needed: Waive the fat target for this dish?" in question
        assert "Blocker: fat 52 g vs limit 40 g (+12 g; basis: ingredient calculation)" in question
        assert "A (recommended). Waive fat — [nutrition-fat] approved by Marco" in question
        assert "B. Trim the oil — Reduce frying oil to 20 g and re-verify" in question
        assert "Repairs considered: Leaner cut changes the dish's Purpose" in question


def test_letter_choice_records_option_text_in_decisions(workflow_db) -> None:  # noqa: F811
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _author, verifier_run, started, _prepared, _inspection = _verification_ready(
            session, ids, context, task_id
        )
        result = port.execute(
            _call(
                "reject",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "route": "human-review",
                    "reason": "The dish exceeds its fat target",
                    "human_review_category": "nutrition_exemption",
                    "human_review_basis": "Waive the fat target for this dish?",
                    "human_review_options": OPTIONS,
                    **BLOCKER,
                },
            )
        )
        assert result.ok, result.data
        agent_run = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=agent_run,
            owner="agent-owner",
            agent="codex",
        )
        decided = port.execute(
            _call(
                "record-human-decision",
                run_id=agent_run,
                request_id=_next(ids),
                owner="agent-owner",
                principal="agent",
                arguments={**result.data["agent_action"]["arguments"], "decision": "A"},
            )
        )
        assert decided.ok, decided.data
        state = session.get(models.DishState, (context["generation_id"], task_id))
        resumed = session.get(models.ContentVersion, state.current_content_version_id)
        decisions = parse_canonical_document(
            title=resumed.title, body=resumed.body, expected_status="pending-verification"
        ).document.decisions
        assert any(
            line.startswith(
                "Human — Marco: human_review resolved — "
                "A: Waive fat — [nutrition-fat] approved by Marco"
            )
            for line in decisions
        )


def _hold_then_decide(session, ids, context, task_id, *, options, decision):
    port, _author, verifier_run, started, _prepared, _inspection = _verification_ready(
        session, ids, context, task_id
    )
    held = port.execute(
        _call(
            "reject",
            run_id=verifier_run,
            request_id=_next(ids),
            owner="verifier-owner",
            principal="verification",
            arguments={
                "task_id": str(task_id),
                "operation_id": started.data["operation_id"],
                "agent": "codex",
                "route": "human-review",
                "reason": "The dish exceeds its fat target",
                "human_review_category": "nutrition_exemption",
                "human_review_basis": "Waive the fat target for this dish?",
                "human_review_options": options,
                **BLOCKER,
            },
        )
    )
    assert held.ok, held.data
    agent_run = _next(ids)
    _register_run(
        session,
        generation_id=context["generation_id"],
        run_id=agent_run,
        owner="agent-owner",
        agent="codex",
    )
    decided = port.execute(
        _call(
            "record-human-decision",
            run_id=agent_run,
            request_id=_next(ids),
            owner="agent-owner",
            principal="agent",
            arguments={**held.data["agent_action"]["arguments"], "decision": decision},
        )
    )
    return held, decided


def _resumed_decisions(session, context, task_id) -> list[str]:
    state = session.get(models.DishState, (context["generation_id"], task_id))
    resumed = session.get(models.ContentVersion, state.current_content_version_id)
    return parse_canonical_document(
        title=resumed.title, body=resumed.body, expected_status="pending-verification"
    ).document.decisions


@pytest.mark.parametrize("letter", ["C", "z"])
def test_letter_matching_no_option_is_rejected(
    workflow_db, letter  # noqa: F811
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        held, decided = _hold_then_decide(
            session, ids, context, task_id, options=OPTIONS, decision=letter
        )
        assert not decided.ok
        assert decided.code == "HUMAN_DECISION_UNKNOWN_OPTION"
        assert decided.data["valid_options"] == ["A", "B"]
        requirement = session.get(
            wf.HumanReviewRequirement, uuid.UUID(held.data["requirement_id"])
        )
        assert requirement.state == "open"


def test_free_text_decision_still_records_with_options(workflow_db) -> None:  # noqa: F811
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _held, decided = _hold_then_decide(
            session, ids, context, task_id, options=OPTIONS, decision="Trim to 30 g instead"
        )
        assert decided.ok, decided.data
        assert any(
            line.startswith("Human — Marco: human_review resolved — Trim to 30 g instead")
            for line in _resumed_decisions(session, context, task_id)
        )


def test_multiline_option_text_is_collapsed_into_one_line(workflow_db) -> None:  # noqa: F811
    factory, ids, context, task_id = workflow_db
    options = [
        {"label": "Waive\n  fat", "decision": "[nutrition-fat] approved\nby Marco"},
        OPTIONS[1],
    ]
    with session_scope(factory) as session:
        held, decided = _hold_then_decide(
            session, ids, context, task_id, options=options, decision="a"
        )
        assert held.data["human_review_options"][0]["label"] == "Waive fat"
        requirement = session.get(
            wf.HumanReviewRequirement, uuid.UUID(held.data["requirement_id"])
        )
        assert "A (recommended). Waive fat — [nutrition-fat] approved by Marco" in (
            requirement.question
        )
        assert decided.ok, decided.data
        assert any(
            line.startswith(
                "Human — Marco: human_review resolved — "
                "A: Waive fat — [nutrition-fat] approved by Marco"
            )
            for line in _resumed_decisions(session, context, task_id)
        )


@pytest.mark.parametrize(
    "category", ["reverse_authenticated_decision", "governed_authorization"]
)
def test_non_nutrition_categories_do_not_require_quantified_blocker(
    workflow_db, category  # noqa: F811
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        result, operation, open_reviews = _reject(
            session,
            ids,
            context,
            task_id,
            human_review_category=category,
            human_review_basis="Reverse the Lock on the cut?",
            human_review_options=[{"label": "Keep", "decision": "Keep the Lock"}],
        )
        assert result.ok, result.data
        assert result.data["quantified_blocker"] is None
        assert open_reviews == 1
        assert operation.phase == "held_human"


def test_human_review_action_schemas_require_category_and_omit_pg_authorization() -> None:
    def human_variant(schema):
        return next(
            variant
            for variant in schema["oneOf"]
            if variant["properties"]["route"]["const"] == "human-review"
        )

    # Preflight fields stay schema-optional so an agent with an out-of-scope question
    # receives Dish's instructive redirect instead of inventing a category to pass
    # client-side schema validation.
    shared = human_variant(action_openapi_argument_schema("reject"))
    assert "human_review_category" not in shared["required"]
    assert shared["properties"]["human_review_category"]["enum"] == list(HUMAN_REVIEW_CATEGORIES)
    postgres = human_variant(postgres_action_argument_schema("reject"))
    assert "human_review_category" in postgres["properties"]
    items = postgres["properties"]["human_review_options"]["items"]
    assert "authorization" not in items["properties"]
    assert postgres["properties"]["resume_status"]["enum"] == ["pending-verification"]
