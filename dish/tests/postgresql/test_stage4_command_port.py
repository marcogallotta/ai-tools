from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_contract import ACTION_COMMANDS
from dish_pg.command_port import CommandCall, PostgresCommandPort, _task_reference_from_dish
from dish_pg.document_authority import parse_canonical_document
from dish_pg.database import session_scope
from dish_pg.openapi import postgres_action_openapi
from dish_pg.planner import (
    AuthorityFence,
    AuthoritativeSnapshot,
    CanonicalCommandIntent,
    EffectObservation,
    adjudicate_effect,
    plan_command,
)
from dish_pg.protocol import AuthenticationError, PostgresProtocolService, ScopedBearerAuthenticator
from dish_pg.read_model import InvalidCursor
from dish_pg.transition import ProjectionService
from dish_tool.models import material_editor_line
from dish_tool.workflow_policy import WorkflowSnapshot, legal_actions
from tests.support.canonical import TASK
from tests.support.postgresql.core import _import_one
from tests.support.postgresql.command import (
    _add_destination_section,
    _add_verification_queue,
    _call,
    _inspect,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db

SECRET = b"stage-4-cursor-secret-32-bytes!!"
ROOT = Path(__file__).resolve().parents[2]


def test_stage4_postgresql_action_path_contract() -> None:
    document = postgres_action_openapi()
    assert set(document["paths"]) == {f"/v1/action/{name}" for name in ACTION_COMMANDS}
    assert document["paths"]["/v1/action/inspect"]["post"]["x-openai-isConsequential"] is True
    checked_in = json.loads((ROOT / "openapi/dish-postgresql-action.openapi.json").read_text())
    assert checked_in == document


@pytest.mark.parametrize(
    ("dish_value", "expected"),
    [
        ("123456789", "123456789"),
        (
            "https://app.asana.com/1/1200569426771227/project/1217084805070730/task/123456789",
            "123456789",
        ),
        ("e55e1667-2a0a-545a-a191-091738d9c347", "e55e1667-2a0a-545a-a191-091738d9c347"),
        ("", None),
        # malformed/unsupported shapes must not accidentally resolve to a task
        ("https://app.asana.com/1/bad/url/shape", None),
        ("/dishes/not-a-uuid/some-slug", None),
        ("https://example.com/nonsense", None),
        # an arbitrary string is passed through unchanged rather than dropped,
        # so resolve_task's own not-found handling still applies to it
        ("totally-unrelated-string", "totally-unrelated-string"),
    ],
)
def test_task_reference_from_dish_reduces_known_shapes(dish_value, expected) -> None:
    assert _task_reference_from_dish(dish_value) == expected


def _signed_ready_baseline(session, ids, context, task_id):
    _add_verification_queue(session, ids, context)
    _add_destination_section(session, ids, context)
    author_run = _next(ids)
    verifier_run = _next(ids)
    _register_run(session, generation_id=context["generation_id"], run_id=author_run)
    _register_run(
        session,
        generation_id=context["generation_id"],
        run_id=verifier_run,
        owner="verifier-owner",
        agent="codex",
    )
    port = _port(session, ids)
    started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
    prepared = _prepare_for_verification(
        port,
        ids,
        task_id=task_id,
        operation_id=started.data["operation_id"],
        run_id=author_run,
    )
    _start_verification(
        port,
        ids,
        task_id=task_id,
        operation_id=started.data["operation_id"],
        run_id=verifier_run,
    )
    _inspect(
        port,
        ids,
        task_id=task_id,
        operation_id=started.data["operation_id"],
        run_id=verifier_run,
    )
    reviewed = session.get(
        models.ContentVersion, uuid.UUID(prepared.data["content_version_id"])
    )
    approved = port.execute(
        _call(
            "approve",
            run_id=verifier_run,
            request_id=_next(ids),
            owner="verifier-owner",
            principal="verification",
            arguments={
                "task_id": str(task_id),
                "operation_id": started.data["operation_id"],
                "agent": "codex",
                "model": "test-verifier",
                "correction": "none",
                "reviewed_identity": reviewed.content_identity,
                "semantic_review_complete": True,
                "provenance_complete": True,
            },
        )
    )
    assert approved.ok, (approved.code, approved.http_status, approved.data)
    submitted = port.execute(
        _call(
            "submit",
            run_id=author_run,
            request_id=_next(ids),
            arguments={
                "task_id": str(task_id),
                "operation_id": started.data["operation_id"],
            },
        )
    )
    assert submitted.ok, (submitted.code, submitted.http_status, submitted.data)
    signed = session.get(
        models.ContentVersion, uuid.UUID(approved.data["signed_content_version_id"])
    )
    return port, signed


def test_prepare_stamps_researched_by_and_self_verified_from_agent(workflow_db) -> None:
    """PG must own tool-owned process fields on initial prepare, like legacy.

    Reproduces the exact PROD shape: a candidate with non-canonical actor-line
    grammar *and* "Verification protocol release: None" (both present in the
    real failing gpt submission). Legacy's initial ``prepare`` deterministically
    overwrites "Status detail", "Resume status", "Verification protocol
    release", "Verified by", "Researched by", and "Self-verified" from
    known/computed values regardless of what the candidate wrote there; PG
    must do the same rather than validating the caller's self-reported text
    as-is.
    """
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        author_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run, agent="gpt")
        malformed_candidate = TASK.replace(
            "Researched by: ChatGPT — GPT-5, 2026-07-25",
            "Researched by: gpt — gpt-5.6-sol, 2026-08-12",
        ).replace(
            "Self-verified: ChatGPT — GPT-5, 2026-07-25",
            "Self-verified: gpt — gpt-5.6-sol, 2026-08-12",
        ).replace(
            "Verification protocol release: abc123",
            "Verification protocol release: None",
        )
        result = port.execute(
            _call(
                "prepare",
                run_id=author_run,
                request_id=_next(ids),
                owner="owner-1",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "file_text": malformed_candidate,
                    "agent": "gpt",
                    "model": "gpt-5.6-sol",
                },
            )
        )
        assert result.ok, (result.code, result.http_status, result.data)

        version = session.get(
            models.ContentVersion, uuid.UUID(result.data["content_version_id"])
        )
        assert "Verification protocol release: None" not in version.body
        assert "Researched by: gpt — gpt-5.6-sol, 2026-08-12" not in version.body
        assert "Self-verified: gpt — gpt-5.6-sol, 2026-08-12" not in version.body
        assert "Verified by: None" in version.body
        assert "Status detail: None" in version.body
        assert "Resume status: None" in version.body


def test_prepare_material_change_stamps_pending_verification_from_signed_baseline(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, signed = _signed_ready_baseline(session, ids, context, task_id)
        signed_parts = parse_canonical_document(title=signed.title, body=signed.body)
        change_run = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=change_run,
            agent="gpt",
        )
        started = port.execute(
            _call(
                "start",
                run_id=change_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "change",
                    "agent": "gpt",
                    "change_level": "small",
                    "change_reason": "tighten the success target",
                },
            )
        )
        assert started.ok, (started.code, started.http_status, started.data)
        candidate = (signed.title + "\n" + signed.body).replace(
            "Crisp and aromatic.",
            "Crisp, aromatic, and deeply browned.",
        )
        result = port.execute(
            _call(
                "prepare",
                run_id=change_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "file_text": candidate,
                    "agent": "gpt",
                    "model": "gpt-5.6-sol",
                    "material_classification": "non-material",
                },
            )
        )
        assert result.ok, (result.code, result.http_status, result.data)
        assert result.data["handoff"] == "verification"
        assert result.data["material_classification"]["requested"] == "non-material"
        assert result.data["material_classification"]["effective"] == "material"
        assert result.data["material_classification"]["forced_material_reasons"]

        version = session.get(
            models.ContentVersion, uuid.UUID(result.data["content_version_id"])
        )
        prepared = parse_canonical_document(title=version.title, body=version.body)
        assert prepared.document.state.values["Status"] == "pending-verification"
        assert (
            prepared.document.state.values["Researched by"]
            == signed_parts.document.state.values["Researched by"]
        )
        assert prepared.document.state.values["Verified by"] == "None"
        assert prepared.document.state.values["Self-verified"] == material_editor_line(
            "gpt", "gpt-5.6-sol", NOW.date().isoformat()
        )
        assert len(prepared.document.material_changes) == len(
            signed_parts.document.material_changes
        ) + 1
        assert "tighten the success target" in prepared.document.material_changes[-1]
        operation = session.get(
            wf.WorkflowOperation, uuid.UUID(started.data["operation_id"])
        )
        assert operation.lifecycle == "open"
        assert operation.phase == "await_verification"
        assert session.scalar(
            select(func.count())
            .select_from(wf.VerificationCycle)
            .where(wf.VerificationCycle.operation_id == operation.operation_id)
        ) == 1


def test_prepare_non_material_change_preserves_prior_signed_state_and_checks_in(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, signed = _signed_ready_baseline(session, ids, context, task_id)
        signed_parts = parse_canonical_document(title=signed.title, body=signed.body)
        placement_before = session.get(
            models.CurrentTaskSectionPlacement,
            (context["generation_id"], task_id),
        ).section_id
        change_run = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=change_run,
            agent="gpt",
        )
        started = port.execute(
            _call(
                "start",
                run_id=change_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "change",
                    "agent": "gpt",
                    "change_level": "small",
                    "change_reason": "preserve last-minute handling",
                },
            )
        )
        assert started.ok, (started.code, started.http_status, started.data)
        candidate = (signed.title + "\n" + signed.body).replace(
            "1. Cook it.",
            "1. Cook it.\n2. Finish fresh just before serving.",
        ).replace(
            "Status: ready",
            "Status: pending-verification",
        )
        result = port.execute(
            _call(
                "prepare",
                run_id=change_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "file_text": candidate,
                    "agent": "gpt",
                    "model": "gpt-5.6-sol",
                    "material_classification": "non-material",
                },
            )
        )
        assert result.ok, (result.code, result.http_status, result.data)
        assert result.data["handoff"] == "checked-in"
        assert result.data["cycle_id"] is None
        assert result.data["placement_projection_event_id"] is None
        assert result.data["material_classification"] == {
            "classified_subject": "canonical body diff from the signed baseline",
            "requested": "non-material",
            "effective": "non-material",
            "forced_material_reasons": [],
            "route": "signed-check-in",
        }

        version = session.get(
            models.ContentVersion, uuid.UUID(result.data["content_version_id"])
        )
        checked_in = parse_canonical_document(title=version.title, body=version.body)
        assert checked_in.document.state == signed_parts.document.state
        assert checked_in.document.material_changes == signed_parts.document.material_changes
        assert "Finish fresh just before serving." in checked_in.body
        operation = session.get(
            wf.WorkflowOperation, uuid.UUID(started.data["operation_id"])
        )
        assert operation.lifecycle == "completed"
        assert operation.phase == "completed"
        assert operation.terminal_outcome == "non_material_checkin"
        assert operation.persisted_actions == []
        assert session.scalar(
            select(func.count())
            .select_from(wf.VerificationCycle)
            .where(wf.VerificationCycle.operation_id == operation.operation_id)
        ) == 0
        originating_signoff = session.scalar(
            select(wf.VerificationSignoff).where(
                wf.VerificationSignoff.task_id == task_id,
                wf.VerificationSignoff.signed_content_version_id
                == signed.content_version_id,
            )
        )
        assert originating_signoff is not None
        checkin_step = session.scalar(
            select(wf.OperationStep)
            .where(wf.OperationStep.operation_id == operation.operation_id)
            .order_by(wf.OperationStep.step_sequence.desc())
            .limit(1)
        )
        assert checkin_step is not None
        assert checkin_step.evidence["inherited_signoff_id"] == str(
            originating_signoff.signoff_id
        )
        assert checkin_step.evidence["inherited_signoff_cycle_id"] == str(
            originating_signoff.cycle_id
        )
        placement_after = session.get(
            models.CurrentTaskSectionPlacement,
            (context["generation_id"], task_id),
        ).section_id
        assert placement_after == placement_before

        second_run = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=second_run,
            agent="gpt",
        )
        second_started = port.execute(
            _call(
                "start",
                run_id=second_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "change",
                    "agent": "gpt",
                    "change_level": "small",
                    "change_reason": "keep the finish crisp",
                },
            )
        )
        assert second_started.ok, (
            second_started.code,
            second_started.http_status,
            second_started.data,
        )
        second_candidate = (version.title + "\n" + version.body).replace(
            "2. Finish fresh just before serving.",
            "2. Finish fresh just before serving.\n3. Keep crisp until serving.",
        )
        second = port.execute(
            _call(
                "prepare",
                run_id=second_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": second_started.data["operation_id"],
                    "file_text": second_candidate,
                    "agent": "gpt",
                    "model": "gpt-5.6-sol",
                    "material_classification": "non-material",
                },
            )
        )
        assert second.ok, (second.code, second.http_status, second.data)
        assert second.data["handoff"] == "checked-in"
        second_version = session.get(
            models.ContentVersion, uuid.UUID(second.data["content_version_id"])
        )
        second_checked_in = parse_canonical_document(
            title=second_version.title, body=second_version.body
        )
        assert second_checked_in.document.state == signed_parts.document.state
        second_operation = session.get(
            wf.WorkflowOperation, uuid.UUID(second_started.data["operation_id"])
        )
        second_step = session.scalar(
            select(wf.OperationStep)
            .where(wf.OperationStep.operation_id == second_operation.operation_id)
            .order_by(wf.OperationStep.step_sequence.desc())
            .limit(1)
        )
        assert second_step is not None
        assert second_step.evidence["inherited_signoff_id"] == str(
            originating_signoff.signoff_id
        )
        assert second_step.evidence["inherited_signoff_cycle_id"] == str(
            originating_signoff.cycle_id
        )



def test_inspect_resolves_task_from_dish_argument(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        author_run = _next(ids)
        verifier_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=verifier_run,
            owner="verifier-owner",
            agent="codex",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        _prepare_for_verification(
            port, ids, task_id=task_id, operation_id=started.data["operation_id"], run_id=author_run
        )
        _start_verification(
            port, ids, task_id=task_id, operation_id=started.data["operation_id"], run_id=verifier_run
        )
        result = port.execute(
            _call(
                "inspect",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "dish": "123456789",
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "independence_attestation": "I independently inspected this exact candidate.",
                },
            )
        )
    assert result.ok, (result.code, result.http_status, result.data)


def test_inspect_without_task_reference_still_requires_one(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        result = port.execute(
            _call(
                "inspect",
                run_id=run_id,
                request_id=_next(ids),
                owner="owner-1",
                principal="verification",
                arguments={"agent": "codex"},
            )
        )
    assert not result.ok
    assert result.code == "TASK_REQUIRED"


def test_inspect_with_unresolvable_dish_argument_does_not_accidentally_resolve(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        result = port.execute(
            _call(
                "inspect",
                run_id=run_id,
                request_id=_next(ids),
                owner="owner-1",
                principal="verification",
                arguments={"dish": "https://example.com/nonsense", "agent": "codex"},
            )
        )
    assert not result.ok
    assert result.code == "TASK_REQUIRED"


@pytest.mark.parametrize(
    ("command", "principal"),
    [
        ("prepare", "agent"),
        ("reject", "verification"),
        ("approve", "verification"),
        ("submit", "agent"),
    ],
)
def test_postgresql_planner_matches_shared_legal_actions(command: str, principal: str) -> None:
    workflow = WorkflowSnapshot(
        operation_status="open",
        operation_phase="prepare_required",
        persisted_actions=("prepare", "reject"),
        live_status="pending-research",
        live_section_gid="rq",
        verification_queue_gid="vq",
        verifier_established=False,
        latest_cycle_outcome=None,
        latest_cycle_route=None,
        validation_rules=(),
        operation_kind="initial",
    )
    allowed = legal_actions(workflow)
    snapshot = AuthoritativeSnapshot(
        generation_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        fence=AuthorityFence(1, 1, 1, 1, 1, "prepare_required"),
        workflow=workflow,
        task_exists=True,
    )

    plan = plan_command(
        snapshot=snapshot,
        intent=CanonicalCommandIntent(command, {}, principal, "owner", str(uuid.uuid4())),
        pinned_now=NOW,
    )

    assert plan.legal is (command in allowed)
    if not plan.legal:
        assert plan.result_code == "ACTION_NOT_LEGAL"
        assert plan.recovery_guidance["allowed_actions"] == tuple(allowed)


def test_planner_delegates_legality_and_adjudicates_exact_effects() -> None:
    workflow = WorkflowSnapshot(
        operation_status="open",
        operation_phase="prepare_required",
        persisted_actions=("prepare",),
        live_status="pending-research",
        live_section_gid="rq",
        verification_queue_gid="vq",
        verifier_established=False,
        latest_cycle_outcome=None,
        latest_cycle_route=None,
        validation_rules=(),
        operation_kind="initial",
    )
    snapshot = AuthoritativeSnapshot(
        generation_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        fence=AuthorityFence(1, 1, 1, 1, 1, "prepare_required"),
        workflow=workflow,
        task_exists=True,
    )
    plan = plan_command(
        snapshot=snapshot,
        intent=CanonicalCommandIntent("prepare", {}, "agent", "owner", str(uuid.uuid4())),
        pinned_now=NOW,
    )
    assert plan.legal is True
    assert [mutation.kind for mutation in plan.mutations] == [
        "activate_content_version",
        "place_verification_queue",
        "append_operation_step",
        "open_verification_cycle",
        "advance_operation",
    ]
    confirmed = adjudicate_effect(
        intended_identity="intent-1",
        observation=EffectObservation("intent-1", "intent-1", True, True, True, {"reread": 1}),
    )
    uncertain = adjudicate_effect(
        intended_identity="intent-1",
        observation=EffectObservation("intent-1", None, None, False, False, {}),
    )
    assert (confirmed.outcome, confirmed.retry_safe) == ("confirmed", False)
    assert (uncertain.outcome, uncertain.retry_safe) == ("uncertain", False)


def test_authoritative_sections_and_registry_bound_pagination(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        _import_one(session, ids, context, asana_gid="123456790")
        _import_one(session, ids, context, asana_gid="123456791")
        port = _port(session, ids)
        sections = port.execute(_call("sections", run_id=_next(ids)))
        assert sections.ok and sections.data["sections"][0]["workflow_role"] == "research_queue"
        first = port.execute(
            _call(
                "section-tasks",
                run_id=_next(ids),
                arguments={"section_gid": "1217084805070731", "page_size": 2},
            )
        )
        assert len(first.data["tasks"]) == 2
        assert first.data["next_cursor"]
        second = port.execute(
            _call(
                "section-tasks",
                run_id=_next(ids),
                arguments={
                    "section_gid": "1217084805070731",
                    "page_size": 2,
                    "cursor": first.data["next_cursor"],
                },
            )
        )
        assert len(second.data["tasks"]) == 1
        with pytest.raises(InvalidCursor):
            port.execute(
                _call(
                    "section-tasks",
                    run_id=_next(ids),
                    arguments={
                        "section_gid": "1217084805070731",
                        "page_size": 3,
                        "cursor": first.data["next_cursor"],
                    },
                )
            )


def test_canonical_section_id_lists_tasks_without_section_gid(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port = _port(session, ids)
        result = port.execute(
            _call(
                "section-tasks",
                run_id=_next(ids),
                arguments={"section_id": str(context["section_id"])},
            )
        )

    assert result.ok
    assert [item["dish_id"] for item in result.data["tasks"]] == [str(task_id)]
    assert result.data["tasks"][0]["section_id"] == str(context["section_id"])
    assert result.data["tasks"][0]["task_gid"] == "123456789"


def test_read_resolves_canonical_dish_id_without_task_gid(workflow_db) -> None:
    factory, ids, _context, task_id = workflow_db
    with session_scope(factory) as session:
        port = _port(session, ids)
        result = port.execute(
            _call("read", run_id=_next(ids), arguments={"dish_id": str(task_id)})
        )

    assert result.ok
    assert result.data["dish_id"] == str(task_id)
    assert result.data["identity_binding"] == {
        "dish_id": str(task_id),
        "task_gid": "123456789",
    }


def test_unknown_canonical_read_and_section_ids_fail_locally(workflow_db) -> None:
    factory, ids, _context, _task_id = workflow_db
    unknown_dish = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    unknown_section = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    with session_scope(factory) as session:
        port = _port(session, ids)
        read_result = port.execute(
            _call("read", run_id=_next(ids), arguments={"dish_id": str(unknown_dish)})
        )
        section_result = port.execute(
            _call(
                "section-tasks",
                run_id=_next(ids),
                arguments={"section_id": str(unknown_section)},
            )
        )

    assert read_result.ok is False
    assert (read_result.code, read_result.http_status) == ("DISH_NOT_FOUND", 404)
    assert read_result.data == {"dish_reference": str(unknown_dish)}
    assert section_result.ok is False
    assert (section_result.code, section_result.http_status) == ("SECTION_NOT_FOUND", 404)
    assert section_result.data == {"section_reference": str(unknown_section)}


def test_start_resolves_canonical_dish_id_without_task_gid(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        result = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"dish_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )

    assert result.ok, (result.code, result.http_status, result.data)
    assert result.data["operation_id"]


def test_legacy_task_gid_alias_resolves_locally_for_start(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        result = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"task_gid": "123456789", "kind": "initial", "agent": "claude"},
            )
        )

    assert result.ok, (result.code, result.http_status, result.data)


def test_safe_reclaim_is_postgresql_native_different_run_recovery(workflow_db, monkeypatch) -> None:
    factory, ids, context, task_id = workflow_db
    source_run = _next(ids)
    successor_run = _next(ids)
    from dish_tool import backend as asana_backend

    monkeypatch.setattr(
        asana_backend.AsanaBackend,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PostgreSQL safe-reclaim must not construct Asana")
        ),
    )
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=source_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=successor_run,
            owner="successor-owner",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=source_run)
        later = NOW + timedelta(minutes=11)
        same_run = port.execute(
            CommandCall(
                command_name="safe-reclaim",
                arguments={
                    "submission_id": started.data["operation_id"],
                    "lease_id": started.data["lease_id"],
                    "agent": "claude",
                },
                owner_id="owner-1",
                principal_class="agent",
                run_id=source_run,
                request_id=_next(ids),
                now=later,
            )
        )
        assert not same_run.ok
        assert same_run.code == "SAFE_RECLAIM_REQUIRES_DIFFERENT_RUN"

        reclaimed = port.execute(
            CommandCall(
                command_name="safe-reclaim",
                arguments={
                    "submission_id": started.data["operation_id"],
                    "lease_id": started.data["lease_id"],
                    "agent": "claude",
                },
                owner_id="successor-owner",
                principal_class="agent",
                run_id=successor_run,
                request_id=_next(ids),
                now=later,
            )
        )
        assert reclaimed.ok, (reclaimed.code, reclaimed.http_status, reclaimed.data)
        successor_id = uuid.UUID(reclaimed.data["successor_operation_id"])
        source = session.get(wf.WorkflowOperation, uuid.UUID(started.data["operation_id"]))
        successor = session.get(wf.WorkflowOperation, successor_id)
        edge = session.scalar(
            select(wf.OperationSuccessionEdge).where(
                wf.OperationSuccessionEdge.source_operation_id == source.operation_id
            )
        )
        assert source.lifecycle == "abandoned"
        assert source.terminal_outcome == "safe_reclaimed"
        assert successor.lifecycle == "open"
        assert successor.predecessor_operation_id == source.operation_id
        assert edge.successor_operation_id == successor.operation_id
        assert reclaimed.data["agent_action"]["arguments"]["prepared_operation_id"] == str(successor_id)

        source_reclaim = port.execute(
            CommandCall(
                command_name="start",
                arguments={
                    "dish_id": str(task_id),
                    "agent": "claude",
                    "kind": "initial",
                    "prepared_operation_id": str(successor_id),
                },
                owner_id="owner-1",
                principal_class="agent",
                run_id=source_run,
                request_id=_next(ids),
                now=later,
            )
        )
        assert not source_reclaim.ok
        assert source_reclaim.code == "SAFE_RECLAIM_SOURCE_RUN_FORBIDDEN"

        claimed = port.execute(
            CommandCall(
                command_name="start",
                arguments={
                    "dish_id": str(task_id),
                    "agent": "claude",
                    "kind": "initial",
                    "prepared_operation_id": str(successor_id),
                },
                owner_id="successor-owner",
                principal_class="agent",
                run_id=successor_run,
                request_id=_next(ids),
                now=later,
            )
        )
        assert claimed.ok, (claimed.code, claimed.http_status, claimed.data)
        assert claimed.data["operation_id"] == str(successor_id)
        assert claimed.data["claimed_prepared_successor"] is True


def test_postgresql_proposals_and_apply_proposal_install_exact_authorized_candidate(
    workflow_db, monkeypatch
) -> None:
    factory, ids, context, task_id = workflow_db
    author_run = _next(ids)
    verifier_run = _next(ids)
    admin_run = _next(ids)
    applying_run = _next(ids)
    from dish_tool import backend as asana_backend

    monkeypatch.setattr(
        asana_backend.AsanaBackend,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PostgreSQL proposal commands must not construct Asana")
        ),
    )
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=verifier_run,
            owner="verifier-owner",
            agent="codex",
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="marco",
            agent="marco",
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=applying_run,
            owner="applying-owner",
            agent="gpt",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        prepared = _prepare_for_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=author_run,
        )
        _start_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )
        _inspect(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )
        reviewed = session.get(
            models.ContentVersion, uuid.UUID(prepared.data["content_version_id"])
        )
        candidate_text = (reviewed.title + "\n" + reviewed.body).replace(
            "Purpose: Compare texture",
            "Purpose: Compare texture and aroma",
        )
        rejected = port.execute(
            _call(
                "reject",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "submission_id": started.data["operation_id"],
                    "agent": "codex",
                    "reason": "Marco-governed purpose change needs durable approval",
                    "route": "large",
                    "model": "test-verifier",
                    "file_text": candidate_text,
                    "governed_change_fields": ["Purpose"],
                },
            )
        )
        assert rejected.ok, (rejected.code, rejected.http_status, rejected.data)
        assert rejected.data["semantic_proposal_queued"] is True
        proposal_id = rejected.data["proposal_id"]
        assert port._current_content_version_id(context["generation_id"], task_id) == reviewed.content_version_id
        assert port.execute(
            _call("proposals", run_id=applying_run, owner="applying-owner")
        ).data["count"] == 0

        for change in rejected.data["required_authorizations"]:
            grant = port.execute(
                _call(
                    "authorize-governed-change",
                    run_id=admin_run,
                    request_id=_next(ids),
                    owner="marco",
                    principal="admin",
                    arguments={
                        "task_id": str(task_id),
                        "operation_id": started.data["operation_id"],
                        "field_name": change["field"],
                        "before": change["before"],
                        "after": change["after"],
                        "reason": "approved exact semantic proposal",
                    },
                )
            )
            assert grant.ok, (grant.code, grant.http_status, grant.data)

        listed = port.execute(
            _call("proposals", run_id=applying_run, owner="applying-owner")
        )
        assert listed.ok
        assert listed.data["count"] == 1
        assert listed.data["proposals"][0]["proposal_id"] == proposal_id

        applied = port.execute(
            _call(
                "apply-proposal",
                run_id=applying_run,
                request_id=_next(ids),
                owner="applying-owner",
                arguments={
                    "proposal_id": proposal_id,
                    "agent": "gpt",
                    "model": "test-model",
                },
            )
        )
        assert applied.ok, (applied.code, applied.http_status, applied.data)
        assert applied.data["proposal_id"] == proposal_id
        current_id = port._current_content_version_id(context["generation_id"], task_id)
        current = session.get(models.ContentVersion, current_id)
        assert current.content_identity == rejected.data["candidate_identity"]
        assert "Purpose: Compare texture and aroma" in current.body
        requirement = session.get(wf.HumanReviewRequirement, uuid.UUID(proposal_id))
        assert requirement.state == "decided"
        assert port.execute(
            _call("proposals", run_id=applying_run, owner="applying-owner")
        ).data["count"] == 0


def test_create_commits_one_authoritative_bundle_and_exact_replay(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id, request_id = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        call = _call(
            "create",
            run_id=run_id,
            request_id=request_id,
            arguments={"title": "New governed dish"},
        )
        first = port.execute(call)
        replay = port.execute(call)
        created = session.scalar(
            select(models.DishTask).where(models.DishTask.task_id == uuid.UUID(first.data["dish_id"]))
        )
        assert first.ok and created.creation_route == "create"
        assert first.data["dish_id"] == first.data["task_id"]
        assert first.data["section_id"] == str(context["section_id"])
        assert replay.ok and replay.request_replayed is True
        assert session.scalar(
            select(func.count()).select_from(models.DishTask).where(models.DishTask.creation_route == "create")
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        ) == 1


def test_planning_challenge_then_fresh_start_opens_exact_operation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        first = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"task_id": str(task_id), "kind": "planning", "agent": "claude"},
            )
        )
        assert first.code == "CONFIRMATION_REQUIRED"
        assert session.scalar(select(func.count()).select_from(wf.CommandExecution)) == 0
        second = port.execute(
            _call(
                "start",
                run_id=run_id,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "planning",
                    "agent": "claude",
                    "intent_challenge_id": first.data["intent_challenge_id"],
                    "intent_basis": "user_requested",
                },
            )
        )
        operation = session.get(wf.WorkflowOperation, uuid.UUID(second.data["operation_id"]))
        lease = session.get(wf.ServiceLease, uuid.UUID(second.data["lease_id"]))
        assert second.ok and operation.kind == "planning"
        assert lease.operation_id == operation.operation_id and lease.state == "active"
        challenge = session.get(
            wf.PlanningIntentChallenge, uuid.UUID(first.data["intent_challenge_id"])
        )
        assert challenge.state == "consumed"


def test_protocol_authenticates_before_loading_body(workflow_db) -> None:
    factory, ids, _context, _task_id = workflow_db
    loaded = False

    def body_loader():
        nonlocal loaded
        loaded = True
        return {"client": {"run_id": str(_next(ids))}, "arguments": {}}

    with session_scope(factory) as session:
        service = PostgresProtocolService(
            _port(session, ids),
            ScopedBearerAuthenticator(action_token="action-secret", private_token="private-secret"),
        )
        with pytest.raises(AuthenticationError):
            service.handle(
                command_name="sections",
                authorization="Bearer wrong",
                body_loader=body_loader,
                owner_id="owner-1",
                now=NOW,
            )
        assert loaded is False
