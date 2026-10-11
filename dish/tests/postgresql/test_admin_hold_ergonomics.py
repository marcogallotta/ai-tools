"""dish-admin hold-resolution ergonomics against the PostgreSQL authority.

Covers the observed production failure: releasing an Evidence hold on a Dish
with no Asana task needed an operation ID, an ``--expected-task-gid ""`` hack,
and offered no next action from ``inspect``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from dish_pg import models
from dish_pg import postgres_service
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.services import CoreAuthorityService, ImportedTaskSpec
from dish_service import admin_cli
from dish_service.client import DishAdminServiceClient
from dish_service.http import DishHTTPServer
from dish_tool.admin_human import render_admin_result
from dish_tool.errors import DishRuleError
from tests.support.postgresql.command import (
    _call,
    _port,
    _start_initial,
    _verification_ready,
)
from tests.support.postgresql.legacy_evidence_hold import (
    legacy_evidence_reject_enabled,
    run_legacy_preconstruction_evidence_hold,
)
from tests.support.postgresql.runtime_validation import runtime_service
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.thread_teardown import start_server_thread, stop_server
from tests.support.verification import TASK as PENDING_RESEARCH_TASK

__all__ = ["workflow_db"]


def _open_verification_evidence_hold(session, ids, context, task_id):
    port, _author_run, verifier_run, started, _prepared, _inspection = _verification_ready(
        session, ids, context, task_id
    )
    operation_id = started.data["operation_id"]
    with legacy_evidence_reject_enabled():
        rejected = port.execute(
            _call(
                "reject",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": operation_id,
                    "agent": "codex",
                    "route": "evidence",
                    "reason": "Need an exact source for the hydration claim",
                },
            )
        )
    assert rejected.ok, rejected
    return port, operation_id, rejected

def _retire_asana_alias(session, task_id) -> None:
    """Make the fixture Dish one with no Asana task (no active external alias)."""

    for alias in session.scalars(
        select(models.TaskExternalAlias).where(
            models.TaskExternalAlias.task_id == task_id
        )
    ):
        alias.state = "retired"
        alias.retired_at = NOW
    session.flush()


def test_admin_cli_releases_evidence_hold_on_dish_without_asana_task(
    workflow_db, tmp_path: Path, capsys
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _port_unused, operation_id, rejected = _open_verification_evidence_hold(
            session, ids, context, task_id
        )
        _retire_asana_alias(session, task_id)

    service = runtime_service(factory, tmp_path)
    # The PostgreSQL admin hold surface is prod-profile only; this is an
    # in-process test database, never the production service.
    service._profile = "prod"
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="private") as server:
        thread = start_server_thread(server, name="postgres-admin-hold-ergonomics")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        client = DishAdminServiceClient(
            base, token="postgres-admin-token", run_id=str(_next(ids))
        )

        def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
            status = admin_cli.main(["--json", *arguments], application=client)
            return status, json.loads(capsys.readouterr().out)

        try:
            inspect_status, inspected = invoke("inspect", str(task_id))
            supply_status, supplied = invoke(
                "supply-evidence",
                f"/dishes/{task_id}/decorative-slug",
                "--detail",
                "The selected dry route is supported by the hydration source",
            )
            after_status, after = invoke("inspect", str(task_id))
        finally:
            stop_server(server, thread)

    # inspect shows the queue's exact next action with identifiers filled in.
    assert inspect_status == 0, inspected
    data = inspected["data"]
    assert data["problem"] == "Dish is waiting for Marco-supplied evidence."
    assert data["hold_question"] == "Need an exact source for the hydration claim"
    [action] = data["human_actions"]
    assert action["kind"] == "supply-evidence"
    assert action["queue_kind"] == "supply_evidence"
    assert action["requires_input"] == ["detail"]
    assert action["arguments"]["positional"] == [operation_id]
    command = action["shell_command"]
    assert command.startswith(f"dish-admin supply-evidence {operation_id} --detail ")
    assert f"--expected-cycle-id {rejected.data['cycle_id']}" in command
    assert "--expected-hold-identity " in command
    assert "--expected-task-gid" not in command
    assert "--resume-status" not in command

    rendered = render_admin_result(inspected, profile="test")
    assert "Dish is waiting for Marco-supplied evidence." in rendered
    assert "Question: Need an exact source for the hydration claim" in rendered
    assert f"Template: dish-admin supply-evidence {operation_id}" in rendered
    assert "Supply the evidence Dish is waiting for." in rendered

    # A Dish frontend URL target, no task pin, and no resume status just work.
    assert supply_status == 0, supplied
    assert supplied["ok"] is True
    assert supplied["data"]["resume_status"] == "pending-verification"
    # The operator sees exactly which operation the Dish target resolved to.
    assert supplied["data"]["operation_id"] == operation_id
    assert supplied["data"]["resolved_operation_target"] == {
        "target": f"/dishes/{task_id}/decorative-slug",
        "dish_id": str(task_id),
        "operation_id": operation_id,
    }
    supplied_text = render_admin_result(supplied, profile="test")
    assert f"Operation: {operation_id}" in supplied_text
    assert (
        f"Resolved from Dish {task_id} to its single open operation {operation_id}"
        in supplied_text
    )
    assert after_status == 0
    assert "human_actions" not in after["data"]
    with session_scope(factory) as session:
        hold = session.get(wf.EvidenceHold, uuid.UUID(rejected.data["hold_id"]))
        operation = session.get(wf.WorkflowOperation, uuid.UUID(operation_id))
        assert hold.state == "supplied"
        assert operation.phase == "await_verification"


def test_expected_task_gid_accepts_dish_uuid_and_still_rejects_wrong_pin(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, operation_id, rejected = _open_verification_evidence_hold(
            session, ids, context, task_id
        )
        _retire_asana_alias(session, task_id)
        admin_run = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="claude",
        )

        def supply(expected_task_gid: str):
            return port.execute(
                _call(
                    "supply-evidence",
                    run_id=admin_run,
                    request_id=_next(ids),
                    owner="Marco",
                    principal="admin",
                    arguments={
                        "submission_id": operation_id,
                        "detail": "source supplied",
                        "expected_task_gid": expected_task_gid,
                        "expected_cycle_id": rejected.data["cycle_id"],
                    },
                )
            )

        mismatch = supply("123456789")  # the retired Asana GID no longer pins it
        assert mismatch.ok is False
        assert mismatch.code == "HOLD_TASK_MISMATCH"
        assert mismatch.data["dish_id"] == str(task_id)
        assert mismatch.data["task_gid"] is None

        matched = supply(str(task_id))
        assert matched.ok is True, matched
        assert matched.data["resume_status"] == "pending-verification"


def test_omitted_resume_status_is_derived_for_preconstruction_evidence_hold(
    workflow_db,
) -> None:
    factory, ids, context, _fixture_task_id = workflow_db
    author_run, admin_run = _next(ids), _next(ids)
    with session_scope(factory) as session:
        title, body = PENDING_RESEARCH_TASK.split("\n", 1)
        task_id = _next(ids)
        CoreAuthorityService(session, uuid_factory=lambda: _next(ids)).import_task_document(
            generation_id=context["generation_id"],
            import_run_id=context["import_run_id"],
            contract_binding_id=context["binding_id"],
            spec=ImportedTaskSpec(
                task_id=task_id,
                asana_task_gid="123456790",
                title=title,
                body=body,
                identity_scheme="legacy-sha256-v1",
                content_identity=hashlib.sha256((title + "\0" + body).encode()).hexdigest(),
                project_ids=(context["project_id"],),
                section_id=context["section_id"],
                completed=False,
                observed_at=NOW,
            ),
        )
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="claude",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        operation_id = started.data["operation_id"]
        held = run_legacy_preconstruction_evidence_hold(
            port,
            _call(
                "hold-reject",
                run_id=author_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": operation_id,
                    "route": "evidence",
                    "reason": "source evidence is required before construction",
                    "resume_status": "pending-research",
                },
            )
        )
        assert held.ok is True, held

        supplied = port.execute(
            _call(
                "supply-evidence",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "submission_id": operation_id,
                    "detail": "the missing source evidence is now available",
                },
            )
        )
        assert supplied.ok is True, supplied
        assert supplied.data["resume_status"] == "pending-research"


def test_admin_operation_target_resolution_refuses_dish_without_open_operation(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, _context, task_id = workflow_db
    service = runtime_service(factory, tmp_path)
    with pytest.raises(DishRuleError) as missing:
        service._resolve_admin_operation_target(str(task_id))
    assert missing.value.code == "NOT_FOUND"
    assert missing.value.rule == "admin_operation_target_not_found"
    assert "no open workflow operation" in str(missing.value)

    unknown = str(uuid.uuid4())
    with pytest.raises(DishRuleError) as unknown_error:
        service._resolve_admin_operation_target(unknown)
    assert unknown_error.value.code == "NOT_FOUND"
    assert unknown_error.value.rule == "admin_operation_target_not_found"


def test_admin_operation_target_resolution_maps_dish_references_to_operation(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _port_unused, operation_id, _rejected = _open_verification_evidence_hold(
            session, ids, context, task_id
        )
    service = runtime_service(factory, tmp_path)
    for reference in (
        operation_id,
        str(task_id),
        f"/dishes/{task_id}/any-title",
        "123456789",
    ):
        assert service._resolve_admin_operation_target(reference) == operation_id


class _RecordingAdminClient(DishAdminServiceClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, command, arguments=None, **keyword_arguments):
        prepared = dict(arguments or keyword_arguments)
        self.calls.append((command, prepared))
        return {
            "ok": True, "command": command, "code": "OK", "task_gid": None,
            "submission_id": None, "state": None, "retryable": False,
            "allowed_actions": [], "data": {}, "errors": [],
        }


@pytest.mark.parametrize("command", ["supply-evidence", "record-human-decision"])
def test_hold_commands_need_only_target_and_detail(command, capsys) -> None:
    client = _RecordingAdminClient()
    dish_id = str(uuid.uuid4())
    assert admin_cli.main(
        ["--json", command, dish_id, "--detail", "Marco's answer"], application=client
    ) == 0
    capsys.readouterr()
    [(sent_command, sent)] = client.calls
    assert sent_command == command
    assert sent["submission_id"] == dish_id
    assert sent["detail"] == "Marco's answer"
    for omitted in (
        "resume_status",
        "expected_task_gid",
        "expected_cycle_id",
        "expected_hold_identity",
    ):
        assert omitted not in sent

    # The former empty-string hack still means "no task check".
    client.calls.clear()
    assert admin_cli.main(
        ["--json", command, dish_id, "--detail", "x", "--expected-task-gid", ""],
        application=client,
    ) == 0
    capsys.readouterr()
    assert "expected_task_gid" not in client.calls[0][1]


def test_human_action_lookup_accepts_queue_and_admin_kind_spellings() -> None:
    result = {"data": {"human_actions": [{"kind": "supply_evidence", "summary": "s"}]}}
    assert admin_cli._human_action_of_kind(result, "supply-evidence") is not None
    result = {"data": {"human_actions": [{"kind": "supply-evidence", "summary": "s"}]}}
    assert admin_cli._human_action_of_kind(result, "supply_evidence") is not None


def test_abandon_operation_cli_requires_exact_lease_id() -> None:
    parser = admin_cli.build_parser()
    with pytest.raises(DishRuleError) as missing_lease:
        parser.parse_args(["abandon-operation", str(uuid.uuid4())])
    assert missing_lease.value.rule == "invalid_arguments"
    abandon = next(
        action.choices["abandon-operation"]
        for action in parser._actions
        if isinstance(getattr(action, "choices", None), dict)
        and "abandon-operation" in action.choices
    )
    [lease_option] = [a for a in abandon._actions if "--lease-id" in a.option_strings]
    assert lease_option.required is True
    assert "omitted" not in (lease_option.help or "")


@pytest.mark.parametrize(
    ("profile", "prefix"),
    [("prod", "dish-admin "), ("test", "dish-admin --profile test ")],
)
def test_printed_admin_commands_name_non_prod_profile(profile, prefix) -> None:
    operation_id = str(uuid.uuid4())
    supply = postgres_service._admin_action_from_queue(
        {"kind": "supply_evidence", "operation_id": operation_id}, profile=profile
    )
    assert supply["shell_command"].startswith(f"{prefix}supply-evidence {operation_id} ")
    resolved = postgres_service._admin_action_from_queue(
        {"kind": "resolve_verification_hold", "operation_id": operation_id},
        profile=profile,
    )
    assert resolved["shell_command"] == f"{prefix}resolved {operation_id}"
    reopen = postgres_service._admin_action_from_queue(
        {"kind": "reopen_verification_stop", "operation_id": operation_id},
        profile=profile,
    )
    assert reopen["shell_command"].startswith(f"{prefix}reopen {operation_id} ")
    assert reopen["requires_input"] == [
        "category", "before", "after", "editor", "model", "run_id", "file_path", "date"
    ]
    assert "--file" in reopen["shell_command"]
    proposal = postgres_service._admin_action_from_queue(
        {"kind": "semantic_proposal", "operation_id": operation_id}, profile=profile
    )
    assert proposal["shell_command"] == f"{prefix}queue"
    if profile == "prod":
        assert "--profile" not in supply["shell_command"]


def test_resolution_refusal_names_non_prod_profile(workflow_db, tmp_path: Path) -> None:
    factory, _ids, _context, task_id = workflow_db
    service = runtime_service(factory, tmp_path)
    service._profile = "test"
    with pytest.raises(DishRuleError) as missing:
        service._resolve_admin_operation_target(str(task_id))
    assert missing.value.rule == "admin_operation_target_not_found"
    assert "dish-admin --profile test inspect" in str(missing.value)


def test_inspect_still_shows_hold_when_queue_derivation_fails(
    workflow_db, tmp_path: Path, monkeypatch
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _port_unused, operation_id, _rejected = _open_verification_evidence_hold(
            session, ids, context, task_id
        )

    def broken_queue(self, *, task_id=None):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr(postgres_service.PostgresCommandPort, "_queue", broken_queue)
    service = runtime_service(factory, tmp_path)
    result = service.execute_admin("inspect", {"dish": str(task_id)})

    assert result["ok"] is True, result
    data = result["data"]
    assert data["dish_id"] == str(task_id)
    assert data["operation_id"] == operation_id
    assert data["operation_phase"]
    assert "human_actions" not in data
    [warning] = data["warnings"]
    assert "queue derivation failed (RuntimeError)" in warning
    rendered = render_admin_result(result, profile="test")
    assert f"Warning: {warning}" in rendered


class _FakeSession:
    def __init__(self, operation_ids) -> None:
        self._operation_ids = operation_ids

    def begin(self):
        return contextlib.nullcontext()

    def get(self, _model, _key):
        return None  # the target is a Dish UUID, not an operation ID

    def scalars(self, _statement):
        return iter(self._operation_ids)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_operation_target_resolution_refuses_several_open_operations(
    tmp_path: Path, monkeypatch
) -> None:
    # Real fixtures cannot hold two open operations for one Dish, so a fake
    # session/read model drives the refusal branch.
    dish_id = uuid.uuid4()
    operation_ids = [uuid.uuid4(), uuid.uuid4()]

    class _FakeReads:
        def __init__(self, _session, **_kwargs) -> None:
            pass

        def resolve_task(self, _reference):
            return SimpleNamespace(task_id=dish_id)

        def active_generation(self):
            return SimpleNamespace(generation_id=uuid.uuid4())

    monkeypatch.setattr(postgres_service, "PostgresReadModel", _FakeReads)
    service = runtime_service(lambda: _FakeSession(operation_ids), tmp_path)
    with pytest.raises(DishRuleError) as ambiguous:
        service._resolve_admin_operation_target(str(dish_id))
    assert ambiguous.value.code == "CONFLICT"
    assert ambiguous.value.rule == "admin_operation_target_ambiguous"
    assert ambiguous.value.details == {
        "dish_id": str(dish_id),
        "operation_ids": [str(value) for value in operation_ids],
    }


def test_exact_operation_target_reports_no_dish_resolution(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _port_unused, operation_id, _rejected = _open_verification_evidence_hold(
            session, ids, context, task_id
        )
    service = runtime_service(factory, tmp_path)
    assert service._resolve_admin_operation_target_source(operation_id) == (
        operation_id,
        None,
    )
    assert service._resolve_admin_operation_target_source(str(task_id)) == (
        operation_id,
        str(task_id),
    )


def test_admin_inspect_shows_active_lease_for_abandon_operation(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _port, _author_run, _verifier_run, started, _prepared, _inspection = (
            _verification_ready(session, ids, context, task_id)
        )
        operation_id = started.data["operation_id"]
        lease_id = session.scalar(
            select(wf.ServiceLease.lease_id).where(
                wf.ServiceLease.operation_id == uuid.UUID(operation_id),
                wf.ServiceLease.state == "active",
            )
        )
    assert lease_id is not None
    service = runtime_service(factory, tmp_path)
    result = service.execute_admin("inspect", {"dish": str(task_id)})

    assert result["ok"] is True, result
    assert result["data"]["operation_id"] == operation_id
    assert result["data"]["active_lease_id"] == str(lease_id)
    assert f"Active lease: {lease_id}" in render_admin_result(result, profile="test")
    abandon = next(
        action.choices["abandon-operation"]
        for action in admin_cli.build_parser()._actions
        if isinstance(getattr(action, "choices", None), dict)
        and "abandon-operation" in action.choices
    )
    assert "shown by inspect as Active lease" in abandon.format_help()
