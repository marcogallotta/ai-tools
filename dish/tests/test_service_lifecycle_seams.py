"""Narrow lifecycle collaborators remain directly constructible and typed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
import sqlite3
from types import SimpleNamespace
from typing import get_type_hints

import pytest

from dish_service.application import DishService
from dish_service.command_spec import ACTION_COMMANDS
from dish_service.lease_requests import LeaseRequestCoordinator, LeaseRequestServicePort
from dish_service.leases import ServicePrincipal
from dish_service.request_coordinators import (
    ASANA_MUTATION_REPLAY_COMMAND,
    LEGACY_DIRECT,
    MEDIATED_ACTION,
    AdmissionAuthorityReference,
    AdminRequestCoordinator,
    AdminRequestServicePort,
    AgentRequestCoordinator,
    AgentRequestServicePort,
    AsanaMutationAdmissionCoordinator,
    TaskStateFingerprint,
    UpstreamMutationDecision,
)
from dish_service.request_replay import FunctionalRequestReplay, RequestReplayPort
from dish_tool.database import content_identity
from dish_tool.errors import DishRuleError


class _Shadow:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs["call"]()


def _replay() -> FunctionalRequestReplay:
    return FunctionalRequestReplay(
        begin_fn=lambda *_args, **_kwargs: (None, True),
        stored_fn=lambda *_args, **_kwargs: None,
        complete_fn=lambda _conn, *, result, **_kwargs: result,
        pending_fn=lambda command, request_id, **_kwargs: DishRuleError(
            "BACKEND_UNCERTAIN", f"{command}:{request_id}", rule="pending"
        ),
    )


def test_coordinator_constructor_dependencies_are_typed_ports() -> None:
    agent = get_type_hints(AgentRequestCoordinator.__init__)
    admin = get_type_hints(AdminRequestCoordinator.__init__)
    lease = get_type_hints(LeaseRequestCoordinator.__init__)
    assert agent["service"] is AgentRequestServicePort
    assert admin["service"] is AdminRequestServicePort
    assert lease["service"] is LeaseRequestServicePort
    assert lease["replay"] is RequestReplayPort


@pytest.mark.parametrize(
    ("method", "expected_command", "kwargs"),
    (
        ("renew", "renew-lease", {"operation_id": "op", "request_id": "req"}),
        (
            "recover",
            "recover-lease",
            {"operation_id": "op", "reason": "resume", "request_id": "req"},
        ),
        (
            "expire",
            "expire-lease",
            {"lease_id": "lease", "reason": "stop", "request_id": "req"}),
        ),
    ),
)
def test_lease_request_coordinator_can_be_exercised_without_dish_service_graph(
    monkeypatch, method, expected_command, kwargs
) -> None:
    shadow = _Shadow()
    service = SimpleNamespace(_shadow_capture=shadow)
    coordinator = LeaseRequestCoordinator(
        service,
        replay=_replay(),
        initialization_error=lambda exc: DishRuleError(
            "INTERNAL_ERROR", str(exc), rule="init"
        ),
        preserve_error=lambda exc, **_kwargs: exc,
    )
    locked = f"_{method}_locked"
    monkeypatch.setattr(coordinator, locked, lambda *_args, **_kwargs: {"ok": True})
    principal = ServicePrincipal(owner_id="owner", run_id="run")

    if method == "expire":
        result = coordinator.expire(principal, **kwargs)
    else:
        operation_id = kwargs.pop("operation_id")
        result = getattr(coordinator, method)(operation_id, principal, **kwargs)

    assert result == {"ok": True}
    assert shadow.calls[0]["command"] == expected_command


def test_dish_service_remains_the_lifecycle_composition_root() -> None:
    source = inspect.getsource(DishService.__init__)
    assert "FunctionalRequestReplay(" in source
    assert "AgentRequestCoordinator(" in source
    assert "AdminRequestCoordinator(" in source
    assert "LeaseRequestCoordinator(" in source
    for method_name in ("renew_lease", "recover_lease", "expire_lease"):
        method_source = inspect.getsource(getattr(DishService, method_name))
        assert "self._lease_requests" in method_source


class _AgentLifecycleService:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._request_replay = _replay()

    def _initialize_database(self, **_kwargs):
        self.calls.append("initialize")
        return sqlite3.connect(":memory:")

    def _lease_manager(self, _conn):
        return object()

    def _arguments_for_principal(self, _command, arguments, *, run_id):
        self.calls.append("prepare")
        return dict(arguments)

    def _begin_agent_execution(self, _state, *, command, request_id):
        self.calls.append("begin")
        return None

    def _build_agent_application(self, state, *, command, request_id):
        self.calls.append("build")
        state.backend = object()
        state.app = object()

    def _resolve_agent_operation(self, state, *, command):
        self.calls.append("resolve")
        state.operation_id = "operation"

    def _acquire_agent_lease(self, _state, *, command):
        self.calls.append("acquire")

    def _dispatch_agent_command(self, _state, *, command):
        self.calls.append("dispatch")
        return {"ok": True}

    def _finish_agent_result(self, _state, *, command, request_id, result):
        self.calls.append("settle")
        return {**result, "settled": True}

    def _agent_rule_error_result(self, *_args, **_kwargs):
        raise AssertionError("unexpected rule-error route")

    def _close_backend(self, _backend):
        self.calls.append("cleanup")


class _AdminLifecycleService:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._request_replay = _replay()

    def _initialize_database(self, **_kwargs):
        self.calls.append("initialize")
        return sqlite3.connect(":memory:")

    def _prepare_admin_execution_state(self, conn, **kwargs):
        from dish_service.request_coordinators import AdminExecutionState

        self.calls.append("prepare")
        return AdminExecutionState(
            conn=conn,
            principal=kwargs["principal"],
            leases=object(),
            prepared_arguments=dict(kwargs["arguments"]),
            operation_id="operation",
            supplied_run_id=kwargs["principal"].run_id,
            replay=self._request_replay,
        )

    def _begin_admin_execution(self, _state, *, command, request_id):
        self.calls.append("begin")
        return None

    def _build_admin_backend(self, state, *, command, request_id):
        self.calls.append("build")
        state.backend = object()
        return None

    def _acquire_admin_execution_lease(self, _state, *, command):
        self.calls.append("acquire")

    def _dispatch_admin_command(self, _state, *, command, request_id):
        self.calls.append("dispatch")
        return {"ok": True}

    def _finish_admin_result(self, _state, *, command, request_id, result):
        self.calls.append("settle")
        return {**result, "settled": True}

    def _admin_rule_error_result(self, *_args, **_kwargs):
        raise AssertionError("unexpected rule-error route")

    def _close_backend(self, _backend):
        self.calls.append("cleanup")


def test_agent_coordinator_exposes_acquisition_settlement_and_cleanup_order() -> None:
    service = _AgentLifecycleService()
    coordinator = AgentRequestCoordinator(
        service,
        initialization_error=lambda exc: DishRuleError(
            "INTERNAL_ERROR", str(exc), rule="init"
        ),
    )
    result = coordinator._execute_locked(
        "approve",
        {"submission_id": "operation"},
        principal=ServicePrincipal(owner_id="owner", run_id="run"),
        request_id="request",
        explicit_principal=True,
    )
    assert result == {"ok": True, "settled": True}
    assert service.calls == [
        "initialize", "prepare", "begin", "build", "resolve",
        "acquire", "dispatch", "settle", "cleanup",
    ]


def test_admin_coordinator_exposes_acquisition_settlement_and_cleanup_order() -> None:
    service = _AdminLifecycleService()
    coordinator = AdminRequestCoordinator(
        service,
        initialization_error=lambda exc: DishRuleError(
            "INTERNAL_ERROR", str(exc), rule="init"
        ),
    )
    result = coordinator._execute_locked(
        "recover",
        {"submission_id": "operation"},
        principal=ServicePrincipal(owner_id="Marco", run_id="run"),
        request_id="request",
    )
    assert result == {"ok": True, "settled": True}
    assert service.calls == [
        "initialize", "prepare", "begin", "build",
        "acquire", "dispatch", "settle", "cleanup",
    ]


# --- Asana mutation admission / inactive transport -------------------------------

_ADMISSION_NOW = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
_ADMISSION_PROJECT = "project"
_ADMISSION_TASK = "123456789"


class _AdmissionBackend:
    def __init__(self) -> None:
        self.task = {
            "gid": _ADMISSION_TASK,
            "name": "Dish",
            "notes": "Notes",
            "completed": False,
            "modified_at": "2026-08-19T00:00:00.000Z",
            "projects": [{"gid": _ADMISSION_PROJECT}],
            "memberships": [
                {
                    "project": {"gid": _ADMISSION_PROJECT},
                    "section": {"gid": "section"},
                }
            ],
        }
        self.reads = 0

    def read_task(self, task_gid):
        assert task_gid == _ADMISSION_TASK
        self.reads += 1
        return {
            **self.task,
            "projects": [dict(item) for item in self.task["projects"]],
            "memberships": [
                {
                    "project": dict(item["project"]),
                    "section": dict(item["section"]),
                }
                for item in self.task["memberships"]
            ],
        }

    def update_task_content(self, **_kwargs):
        raise AssertionError("admission must not call Asana writes directly")

    def update_task_completed(self, **_kwargs):
        raise AssertionError("admission must not call Asana writes directly")

    def move_task_to_section(self, **_kwargs):
        raise AssertionError("admission must not call Asana writes directly")


class _AdmissionAuthority:
    def __init__(self, decision) -> None:
        self.decision = decision
        self.reads = 0

    def read_mutation_decision(self, *, target_task_gid, action_class):
        self.reads += 1
        return self.decision


class _AdmissionEffect:
    def __init__(self, backend, *, mismatch=False) -> None:
        self.backend = backend
        self.mismatch = mismatch
        self.calls = 0

    def apply(self, *, proposal):
        self.calls += 1
        self.backend.task["completed"] = not self.mismatch


class _ForbiddenReplay:
    def begin(self, *_args, **_kwargs):
        raise AssertionError("inactive transport must not journal a request")


def _authority_ref(source, revision):
    return AdmissionAuthorityReference(
        source=source,
        identity=f"{source}:identity",
        revision=revision,
    )


def _admission_decision(
    *,
    status="PERMITTED",
    decision_revision="lifecycle-r1",
    design=True,
):
    return UpstreamMutationDecision(
        target_task_gid=_ADMISSION_TASK,
        action_class="task.complete",
        status=status,
        decision_ref=_authority_ref("lifecycle-v3", decision_revision),
        supporting_refs=(_authority_ref("asana-v1", "model-r1"),),
        design_generation_ref=(
            _authority_ref("review-v2", "generation-r1") if design else None
        ),
    )


def _state(*, completed):
    identity = content_identity("Dish", "Notes").digest
    return TaskStateFingerprint(identity, "section", completed)


def _admission_setup(*, status="PERMITTED", design=True, effect=None, writes=False, replay=None):
    backend = _AdmissionBackend()
    authority = _AdmissionAuthority(
        _admission_decision(status=status, design=design)
    )
    coordinator = AsanaMutationAdmissionCoordinator(
        backend=backend,
        project_gid=_ADMISSION_PROJECT,
        authority=authority,
        effect=effect,
        writes_enabled=writes,
        replay=replay,
    )
    return backend, authority, coordinator


def _proposal(coordinator, *, design_bearing=False, expected_completed=True):
    admission = coordinator.propose(
        proposal_id="proposal-1",
        target_task_gid=_ADMISSION_TASK,
        action_class="task.complete",
        mutation={"completed": True},
        expected_after=_state(completed=expected_completed),
        design_bearing=design_bearing,
        now=_ADMISSION_NOW,
        ttl_seconds=300,
    )
    assert admission.status == "PROPOSED"
    assert admission.proposal is not None
    return admission.proposal


def _request_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE service_requests(
            request_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            command TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            operation_id TEXT,
            task_gid TEXT,
            result_json TEXT,
            completed_at TEXT,
            resolution_result_json TEXT,
            resolved_at TEXT
        );
        CREATE TABLE operations(operation_id TEXT PRIMARY KEY);
        """
    )
    return conn


def test_asana_mutation_observation_distinguishes_legacy_from_mediated() -> None:
    _backend, _authority, coordinator = _admission_setup()

    legacy = coordinator.observe(
        target_task_gid=_ADMISSION_TASK,
        action_class="task.complete",
        transport_mode=LEGACY_DIRECT,
    )
    mediated = coordinator.propose(
        proposal_id="proposal-1",
        target_task_gid=_ADMISSION_TASK,
        action_class="task.complete",
        mutation={"z": 2, "a": 1},
        expected_after=_state(completed=True),
        design_bearing=True,
        now=_ADMISSION_NOW,
    )

    assert legacy.transport_mode == LEGACY_DIRECT
    assert mediated.status == "PROPOSED"
    assert mediated.proposal is not None
    assert mediated.proposal.transport_mode == MEDIATED_ACTION
    assert mediated.proposal.mutation_json == '{"a":1,"z":2}'
    assert (
        mediated.proposal.upstream.design_generation_ref
        == _authority_ref("review-v2", "generation-r1")
    )


@pytest.mark.parametrize(
    ("status", "design", "design_bearing"),
    [
        ("BLOCKED", True, False),
        ("CONTRADICTORY", True, False),
        ("UNAVAILABLE", True, False),
        ("PERMITTED", False, True),
    ],
)
def test_asana_mutation_proposal_fails_closed_on_upstream_state(
    status, design, design_bearing
) -> None:
    _backend, _authority, coordinator = _admission_setup(
        status=status, design=design
    )

    admission = coordinator.propose(
        proposal_id="proposal-1",
        target_task_gid=_ADMISSION_TASK,
        action_class="task.complete",
        mutation={"completed": True},
        expected_after=_state(completed=True),
        design_bearing=design_bearing,
        now=_ADMISSION_NOW,
    )

    assert admission.status == "BLOCKED_UPSTREAM"
    assert admission.proposal is None


def test_asana_mutation_shadow_rereads_target_and_upstream_before_admission() -> None:
    backend, authority, coordinator = _admission_setup()
    proposal = _proposal(coordinator)

    assert coordinator.shadow_admit(
        proposal, now=_ADMISSION_NOW + timedelta(seconds=1)
    ).status == "WOULD_ADMIT"

    backend.task["notes"] = "changed outside admission"
    assert coordinator.shadow_admit(
        proposal, now=_ADMISSION_NOW + timedelta(seconds=2)
    ).status == "STALE"

    backend.task["notes"] = "Notes"
    authority.decision = _admission_decision(decision_revision="lifecycle-r2")
    assert coordinator.shadow_admit(
        proposal, now=_ADMISSION_NOW + timedelta(seconds=3)
    ).status == "STALE"

    assert coordinator.shadow_admit(
        proposal, now=_ADMISSION_NOW + timedelta(seconds=301)
    ).status == "STALE"


def test_asana_mutation_transport_is_inactive_and_unwired_by_default() -> None:
    backend, authority, coordinator = _admission_setup(
        replay=_ForbiddenReplay(),
    )
    proposal = _proposal(coordinator)
    conn = sqlite3.connect(":memory:")
    try:
        result = coordinator.execute(
            conn,
            proposal,
            principal=ServicePrincipal(owner_id="owner", run_id="run"),
            request_id="request-1",
            now=_ADMISSION_NOW + timedelta(seconds=1),
        )
    finally:
        conn.close()

    assert result["code"] == "WRONG_STATE"
    assert result["errors"][0]["rule"] == "asana_mutation_writes_inactive"
    assert result["data"]["transport_mode"] == MEDIATED_ACTION
    assert ASANA_MUTATION_REPLAY_COMMAND not in ACTION_COMMANDS
    assert "AsanaMutationAdmissionCoordinator(" not in inspect.getsource(
        DishService.__init__
    )
    assert backend.task["completed"] is False
    assert authority.reads == 1  # proposal only; execute performed no admission/read


def test_asana_mutation_active_seam_reuses_service_request_replay_and_exact_readback() -> None:
    backend = _AdmissionBackend()
    authority = _AdmissionAuthority(_admission_decision())
    effect = _AdmissionEffect(backend)
    coordinator = AsanaMutationAdmissionCoordinator(
        backend=backend,
        project_gid=_ADMISSION_PROJECT,
        authority=authority,
        effect=effect,
        writes_enabled=True,
    )
    proposal = _proposal(coordinator)
    conn = _request_db()
    principal = ServicePrincipal(owner_id="owner", run_id="run")
    try:
        first = coordinator.execute(
            conn,
            proposal,
            principal=principal,
            request_id="request-1",
            now=_ADMISSION_NOW + timedelta(seconds=1),
        )
        replayed = coordinator.execute(
            conn,
            proposal,
            principal=principal,
            request_id="request-1",
            now=_ADMISSION_NOW + timedelta(seconds=2),
        )
        row = conn.execute(
            "SELECT status FROM service_requests WHERE request_id='request-1'"
        ).fetchone()
    finally:
        conn.close()

    assert first["ok"] is True
    assert first["data"]["admission"] == "CONFIRMED"
    assert first["data"]["transport_mode"] == MEDIATED_ACTION
    assert replayed["data"]["request_replayed"] is True
    assert effect.calls == 1
    assert row["status"] == "completed"


def test_asana_mutation_active_seam_rejects_stale_precondition_before_effect() -> None:
    backend = _AdmissionBackend()
    authority = _AdmissionAuthority(_admission_decision())
    effect = _AdmissionEffect(backend)
    coordinator = AsanaMutationAdmissionCoordinator(
        backend=backend,
        project_gid=_ADMISSION_PROJECT,
        authority=authority,
        effect=effect,
        writes_enabled=True,
    )
    proposal = _proposal(coordinator)
    backend.task["notes"] = "external change"
    conn = _request_db()
    try:
        result = coordinator.execute(
            conn,
            proposal,
            principal=ServicePrincipal(owner_id="owner", run_id="run"),
            request_id="request-stale",
            now=_ADMISSION_NOW + timedelta(seconds=1),
        )
    finally:
        conn.close()

    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "asana_mutation_proposal_stale"
    assert effect.calls == 0


def test_asana_mutation_readback_mismatch_is_uncertain_not_retried() -> None:
    backend = _AdmissionBackend()
    authority = _AdmissionAuthority(_admission_decision())
    effect = _AdmissionEffect(backend, mismatch=True)
    coordinator = AsanaMutationAdmissionCoordinator(
        backend=backend,
        project_gid=_ADMISSION_PROJECT,
        authority=authority,
        effect=effect,
        writes_enabled=True,
    )
    proposal = _proposal(coordinator)
    conn = _request_db()
    try:
        first = coordinator.execute(
            conn,
            proposal,
            principal=ServicePrincipal(owner_id="owner", run_id="run"),
            request_id="request-uncertain",
            now=_ADMISSION_NOW + timedelta(seconds=1),
        )
        replayed = coordinator.execute(
            conn,
            proposal,
            principal=ServicePrincipal(owner_id="owner", run_id="run"),
            request_id="request-uncertain",
            now=_ADMISSION_NOW + timedelta(seconds=2),
        )
        row = conn.execute(
            "SELECT status FROM service_requests WHERE request_id='request-uncertain'"
        ).fetchone()
    finally:
        conn.close()

    assert first["code"] == "BACKEND_UNCERTAIN"
    assert first["errors"][0]["rule"] == "asana_mutation_readback_mismatch"
    assert replayed["data"]["request_replayed"] is True
    assert effect.calls == 1
    assert row["status"] == "uncertain"
