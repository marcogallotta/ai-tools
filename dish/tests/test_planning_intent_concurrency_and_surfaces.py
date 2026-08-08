from __future__ import annotations

import threading

import pytest

import dish_service.application as service_application
from dish_service.application import DishService
from dish_service.client import DishActionClient, DishServiceClient
from dish_service.command_spec import validate_action_request
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.leases import ServicePrincipal
from dish_service.cli import build_parser
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from tests.support.thread_teardown import join_thread, start_server_thread, stop_server, managed_thread
from tests.support.planning import Backend, release
from tests.support.planning_intent import (
    FIRST_REQUEST,
    RUN_ID,
    SECOND_REQUEST,
    TASK_GID,
    THIRD_REQUEST,
    confirm as _confirm,
    connect as _connect,
    issue as _issue,
    planning_arguments as _planning_arguments,
    principal as _principal,
    service as _service,
)















@pytest.mark.flake_stress
def test_concurrent_exact_first_calls_share_one_durable_challenge(tmp_path):
    service, _ = _service(tmp_path)
    barrier = threading.Barrier(3)
    results: list[dict] = []
    failures: list[BaseException] = []

    def invoke():
        try:
            barrier.wait(timeout=5)
            results.append(_issue(service))
        except BaseException as exc:  # pragma: no cover - assertion reports it
            failures.append(exc)

    threads = [managed_thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        join_thread(thread, timeout=5)
        assert not thread.is_alive(), "Planning challenge worker did not terminate"

    assert not failures
    assert len(results) == 2
    assert {result["code"] for result in results} == {"CONFIRMATION_REQUIRED"}
    assert len({result["data"]["intent_challenge_id"] for result in results}) == 1

    conn = _connect(service)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM planning_intent_challenges"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM service_requests WHERE request_id=?",
            (FIRST_REQUEST,),
        ).fetchone()[0] == 1
    finally:
        conn.close()
class _BlockingPlanningBackend(Backend):
    def __init__(self, entered_read, release_read):
        super().__init__(task_gid=TASK_GID)
        self._entered_read = entered_read
        self._release_read = release_read
        self._read_lock = threading.Lock()
        self._blocked_once = False

    def read_task(self, gid):
        with self._read_lock:
            should_block = not self._blocked_once
            if should_block:
                self._blocked_once = True
        if should_block:
            self._entered_read.set()
            assert self._release_read.wait(timeout=5)
        return super().read_task(gid)


class _ObservedPlanningIntentLock:
    def __init__(self, real_lock, second_lock_attempted):
        self._real_lock = real_lock
        self._second_lock_attempted = second_lock_attempted

    def __enter__(self):
        if threading.current_thread().name == "planning-followup-second":
            self._second_lock_attempted.set()
        self._real_lock.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._real_lock.release()


def _observe_followup_lock(service, challenge_id, second_lock_attempted):
    lock_index = sum(challenge_id.encode("utf-8")) % len(
        service._planning_intent_locks
    )
    locks = list(service._planning_intent_locks)
    locks[lock_index] = _ObservedPlanningIntentLock(
        locks[lock_index], second_lock_attempted
    )
    service._planning_intent_locks = tuple(locks)


def _run_exact_followup_race(service, arguments, entered_read, release_read):
    results: list[dict] = []
    failures: list[BaseException] = []
    second_started = threading.Event()
    second_lock_attempted = threading.Event()
    second_done = threading.Event()
    _observe_followup_lock(
        service, arguments["intent_challenge_id"], second_lock_attempted
    )

    def invoke(*, second=False):
        try:
            if second:
                second_started.set()
            results.append(
                service.execute_agent(
                    "start",
                    arguments,
                    principal=_principal(),
                    request_id=SECOND_REQUEST,
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            failures.append(exc)
        finally:
            if second:
                second_done.set()

    first = managed_thread(target=invoke, name="planning-followup-first")
    first.start()
    assert entered_read.wait(timeout=5)
    second = managed_thread(
        target=invoke, kwargs={"second": True}, name="planning-followup-second"
    )
    second.start()
    assert second_started.wait(timeout=5)
    assert second_lock_attempted.wait(timeout=5), (
        "second exact follow-up never attempted the serialized challenge lock"
    )
    assert not second_done.is_set(), (
        "second exact follow-up completed before the first request released"
    )
    release_read.set()
    for thread in (first, second):
        join_thread(thread, timeout=5)
        assert not thread.is_alive()
    return results, failures


def _assert_followups_converged(service, results, failures):
    assert not failures
    assert len(results) == 2
    assert all(result["ok"] for result in results)
    assert len({result["submission_id"] for result in results}) == 1
    conn = _connect(service)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        row = conn.execute(
            "SELECT status,operation_id FROM planning_intent_challenges"
        ).fetchone()
        assert tuple(row) == ("consumed", results[0]["submission_id"])
    finally:
        conn.close()


@pytest.mark.flake_stress
def test_concurrent_exact_followups_converge_on_one_operation(tmp_path):
    entered_read = threading.Event()
    release_read = threading.Event()
    backend = _BlockingPlanningBackend(entered_read, release_read)
    service, _ = _service(tmp_path, backend=backend)
    challenge = _issue(service)
    arguments = _planning_arguments(
        intent_challenge_id=challenge["data"]["intent_challenge_id"],
        intent_basis="user_requested",
    )
    results, failures = _run_exact_followup_race(
        service, arguments, entered_read, release_read
    )
    assert len(results) == 2
    _assert_followups_converged(service, results, failures)


def test_cli_parser_and_transport_preserve_only_supplied_confirmation_fields():
    first = vars(
        build_parser().parse_args(
            ["start", TASK_GID, "--agent", "gpt", "--kind", "planning"]
        )
    )
    assert first["intent_challenge_id"] is None
    transported = DishServiceClient._transport_arguments(first)
    assert "intent_challenge_id" not in transported
    assert "intent_basis" not in transported
    assert "override_reason" not in transported

    challenge_id = "77777777-7777-4777-8777-777777777777"
    followup = vars(
        build_parser().parse_args(
            [
                "start",
                TASK_GID,
                "--agent",
                "gpt",
                "--kind",
                "planning",
                "--intent-challenge-id",
                challenge_id,
                "--intent-basis",
                "agent_override",
                "--override-reason",
                "Marco asked the agent to proceed proactively",
            ]
        )
    )
    transported = DishServiceClient._transport_arguments(followup)
    assert transported["intent_challenge_id"] == challenge_id
    assert transported["intent_basis"] == "agent_override"
    assert transported["override_reason"] == "Marco asked the agent to proceed proactively"
def test_action_contract_accepts_two_call_planning_shape_and_rejects_other_kinds():
    client = {"run_id": RUN_ID, "request_id": FIRST_REQUEST}
    first = {"client": client, "arguments": _planning_arguments()}
    _client, arguments = validate_action_request("start", first)
    assert arguments == _planning_arguments()

    confirmed = {
        "client": {"run_id": RUN_ID, "request_id": SECOND_REQUEST},
        "arguments": _planning_arguments(
            intent_challenge_id="77777777-7777-4777-8777-777777777777",
            intent_basis="user_requested",
        ),
    }
    _client, arguments = validate_action_request("start", confirmed)
    assert arguments["intent_basis"] == "user_requested"

    with pytest.raises(DishRuleError) as exc:
        validate_action_request(
            "start",
            {
                "client": client,
                "arguments": {
                    "agent": "gpt",
                    "task_gid": TASK_GID,
                    "kind": "initial",
                    "intent_basis": "user_requested",
                },
            },
        )
    assert exc.value.rule == "argument_unexpected"
    assert exc.value.details["field"] == "intent_basis"
def test_private_service_rejects_confirmation_fields_for_nonplanning_start(tmp_path):
    service, _ = _service(tmp_path)
    result = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": TASK_GID,
            "kind": "initial",
            "intent_basis": "user_requested",
        },
        principal=_principal(),
        request_id=FIRST_REQUEST,
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"rule": "argument_unexpected", "field": "intent_basis"}
    ]
@pytest.mark.parametrize(
    ("client_type", "token"),
    [(DishServiceClient, "agent-secret"), (DishActionClient, "action-secret")],
)
def test_live_cli_and_action_http_surfaces_share_two_call_gate(
    tmp_path, client_type, token
):
    service, _ = _service(tmp_path)
    server = build_server(service)
    thread = start_server_thread(server, daemon=True, name="thread")
    host, port = server.server_address
    client = client_type(f"http://{host}:{port}", token=token, run_id=RUN_ID)
    try:
        first = client.execute(
            "start",
            request_id=FIRST_REQUEST,
            agent="gpt",
            task_gid=TASK_GID,
            kind="planning",
        )
        assert first["code"] == "CONFIRMATION_REQUIRED"
        assert first["submission_id"] is None

        second = client.execute(
            "start",
            request_id=SECOND_REQUEST,
            agent="gpt",
            task_gid=TASK_GID,
            kind="planning",
            intent_challenge_id=first["data"]["intent_challenge_id"],
            intent_basis="user_requested",
        )
    finally:
        stop_server(server, thread)

    assert second["ok"], second
    assert second["allowed_actions"] == ["prepare"]
def test_invalid_agent_does_not_create_planning_challenge(tmp_path):
    service, _ = _service(tmp_path)
    result = _issue(service, arguments=_planning_arguments(agent="unknown"))
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "invalid_agent"

    conn = _connect(service)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM planning_intent_challenges"
        ).fetchone()[0] == 0
    finally:
        conn.close()
