"""Narrow lifecycle collaborators remain directly constructible and typed."""
from __future__ import annotations

import inspect
import sqlite3
from types import SimpleNamespace
from typing import get_type_hints

import pytest

from dish_service.application import DishService
from dish_service.lease_requests import LeaseRequestCoordinator, LeaseRequestServicePort
from dish_service.leases import ServicePrincipal
from dish_service.request_coordinators import (
    AdminRequestCoordinator,
    AdminRequestServicePort,
    AgentRequestCoordinator,
    AgentRequestServicePort,
)
from dish_service.request_replay import FunctionalRequestReplay, RequestReplayPort
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
            {"lease_id": "lease", "reason": "stop", "request_id": "req"},
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

