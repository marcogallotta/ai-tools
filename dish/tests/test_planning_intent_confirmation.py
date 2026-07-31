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
from dish_tool.cli import build_parser
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from tests.support.planning import Backend, release


TASK_GID = "123456789"
RUN_ID = "11111111-1111-4111-8111-111111111111"
FIRST_REQUEST = "22222222-2222-4222-8222-222222222222"
SECOND_REQUEST = "33333333-3333-4333-8333-333333333333"
THIRD_REQUEST = "44444444-4444-4444-8444-444444444444"


def _service(tmp_path, backend=None, *, backend_factory=None):
    honest = tmp_path / "honest"
    honest.mkdir()
    (honest / "dish-verification-protocol.md").write_text(
        "verification protocol", encoding="utf-8"
    )
    selected_backend = backend or Backend()
    return DishService(
        ServiceConfig(
            db_path=tmp_path / "dish.db",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=backend_factory or (lambda: selected_backend),
        release_loader=lambda role=None: release(honest, role),
    ), selected_backend


def _principal(owner_id="action", run_id=RUN_ID):
    return ServicePrincipal(owner_id=owner_id, run_id=run_id)


def _planning_arguments(**extra):
    return {"agent": "gpt", "task_gid": TASK_GID, "kind": "planning", **extra}


def _issue(service, *, arguments=None, request_id=FIRST_REQUEST, principal=None):
    return service.execute_agent(
        "start",
        arguments or _planning_arguments(),
        principal=principal or _principal(),
        request_id=request_id,
    )


def _confirm(
    service,
    challenge,
    *,
    request_id=SECOND_REQUEST,
    principal=None,
    intent_basis="user_requested",
    override_reason=None,
    arguments=None,
):
    payload = dict(arguments or _planning_arguments())
    payload.update(
        {
            "intent_challenge_id": challenge["data"]["intent_challenge_id"],
            "intent_basis": intent_basis,
        }
    )
    if override_reason is not None:
        payload["override_reason"] = override_reason
    return service.execute_agent(
        "start",
        payload,
        principal=principal or _principal(),
        request_id=request_id,
    )


def _connect(service):
    return initialize_database(service.config.db_path)


@pytest.mark.smoke
@pytest.mark.invariant_planning_intent
def test_first_planning_start_only_issues_durable_confirmation(tmp_path):
    backend_calls = 0

    def backend_factory():
        nonlocal backend_calls
        backend_calls += 1
        return Backend()

    service, _backend = _service(tmp_path, backend_factory=backend_factory)
    result = _issue(service)

    assert result["code"] == "CONFIRMATION_REQUIRED"
    assert result["retryable"] is True
    assert result["allowed_actions"] == ["start"]
    assert result["submission_id"] is None
    assert result["data"]["request_id"] == FIRST_REQUEST
    assert result["data"]["required_start_kind"] == "planning"
    assert result["data"]["required_intent_basis"] == [
        "user_requested",
        "agent_override",
    ]
    challenge_id = result["data"]["intent_challenge_id"]
    assert result["data"]["planning_intent_confirmation"] == {
        "challenge_id": challenge_id,
        "status": "issued",
        "single_use": True,
        "task_gid": TASK_GID,
    }
    assert backend_calls == 0

    conn = _connect(service)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM service_leases").fetchone()[0] == 0
        row = conn.execute(
            "SELECT * FROM planning_intent_challenges WHERE challenge_id=?",
            (challenge_id,),
        ).fetchone()
        assert row["status"] == "issued"
        assert row["created_request_id"] == FIRST_REQUEST
        assert row["claimed_request_id"] is None
    finally:
        conn.close()


def test_first_call_cannot_bypass_challenge_with_lone_intent_fields(tmp_path):
    service, _ = _service(tmp_path)
    result = _issue(
        service,
        arguments=_planning_arguments(
            intent_basis="agent_override",
            override_reason="Agent decided Planning looked useful",
        ),
    )

    assert result["code"] == "CONFIRMATION_REQUIRED"
    assert result["submission_id"] is None


def test_exact_first_call_replay_returns_same_challenge(tmp_path):
    service, _ = _service(tmp_path)
    first = _issue(service)
    replay = _issue(service)

    assert replay["code"] == "CONFIRMATION_REQUIRED"
    assert replay["data"]["intent_challenge_id"] == first["data"][
        "intent_challenge_id"
    ]
    assert replay["data"]["request_replayed"] is True

    conn = _connect(service)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM planning_intent_challenges").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_fresh_user_requested_confirmation_starts_and_consumes_challenge(tmp_path):
    service, _ = _service(tmp_path)
    challenge = _issue(service)
    started = _confirm(service, challenge)

    assert started["ok"], started
    assert started["allowed_actions"] == ["prepare"]
    assert started["data"]["request_id"] == SECOND_REQUEST

    conn = _connect(service)
    try:
        row = conn.execute(
            "SELECT * FROM planning_intent_challenges WHERE challenge_id=?",
            (challenge["data"]["intent_challenge_id"],),
        ).fetchone()
        assert row["status"] == "consumed"
        assert row["claimed_request_id"] == SECOND_REQUEST
        assert row["intent_basis"] == "user_requested"
        assert row["override_reason"] is None
        assert row["operation_id"] == started["submission_id"]
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM service_leases").fetchone()[0] == 1
    finally:
        conn.close()


def test_agent_override_requires_and_persists_nonblank_reason(tmp_path):
    service, _ = _service(tmp_path)
    challenge = _issue(service)
    missing = _confirm(
        service,
        challenge,
        intent_basis="agent_override",
        override_reason="   ",
    )
    assert missing["code"] == "INVALID_ARGUMENT"
    assert missing["errors"] == [
        {
            "rule": "planning_intent_override_reason_required",
            "field": "override_reason",
        }
    ]

    started = _confirm(
        service,
        challenge,
        request_id=THIRD_REQUEST,
        intent_basis="agent_override",
        override_reason="  Task was explicitly selected for proactive planning  ",
    )
    assert started["ok"], started

    conn = _connect(service)
    try:
        row = conn.execute(
            "SELECT intent_basis,override_reason FROM planning_intent_challenges"
        ).fetchone()
        assert tuple(row) == (
            "agent_override",
            "Task was explicitly selected for proactive planning",
        )
    finally:
        conn.close()


def test_challenge_is_bound_to_exact_principal_task_and_single_followup(tmp_path):
    service, _ = _service(tmp_path)
    challenge = _issue(service)

    wrong_principal = _confirm(
        service,
        challenge,
        principal=_principal(
            owner_id="another-action",
            run_id="55555555-5555-4555-8555-555555555555",
        ),
    )
    assert wrong_principal["code"] == "CONFLICT"
    assert wrong_principal["errors"][0]["rule"] == "planning_intent_challenge_mismatch"

    started = _confirm(service, challenge, request_id=THIRD_REQUEST)
    assert started["ok"], started

    reused = _confirm(
        service,
        challenge,
        request_id="66666666-6666-4666-8666-666666666666",
    )
    assert reused["code"] == "CONFLICT"
    assert reused["errors"][0]["rule"] == "planning_intent_challenge_already_used"


def test_exact_replay_resumes_after_crash_between_claim_and_start(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    challenge = _issue(service)
    arguments = _planning_arguments(
        intent_challenge_id=challenge["data"]["intent_challenge_id"],
        intent_basis="user_requested",
    )
    original = service._build_agent_application
    crashed = False

    def crash_once(state, *, command, request_id):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise SystemExit("crash after durable challenge claim")
        return original(state, command=command, request_id=request_id)

    monkeypatch.setattr(service, "_build_agent_application", crash_once)
    with pytest.raises(SystemExit):
        service.execute_agent(
            "start",
            arguments,
            principal=_principal(),
            request_id=SECOND_REQUEST,
        )

    replay = service.execute_agent(
        "start",
        arguments,
        principal=_principal(),
        request_id=SECOND_REQUEST,
    )
    assert replay["ok"], replay
    assert replay["submission_id"]

    conn = _connect(service)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        row = conn.execute("SELECT status FROM planning_intent_challenges").fetchone()
        assert row["status"] == "consumed"
    finally:
        conn.close()


def test_exact_replay_converges_after_operation_commit_before_result(tmp_path, monkeypatch):
    service, _ = _service(tmp_path)
    challenge = _issue(service)
    arguments = _planning_arguments(
        intent_challenge_id=challenge["data"]["intent_challenge_id"],
        intent_basis="user_requested",
    )
    original_complete = service_application.complete_request
    crashed = False

    def crash_once(conn, *, request_id, result):
        nonlocal crashed
        if result.get("code") == "OK" and not crashed:
            crashed = True
            raise SystemExit("crash before Planning request completion")
        return original_complete(conn, request_id=request_id, result=result)

    monkeypatch.setattr(service_application, "complete_request", crash_once)
    with pytest.raises(SystemExit):
        service.execute_agent(
            "start",
            arguments,
            principal=_principal(),
            request_id=SECOND_REQUEST,
        )

    replay = service.execute_agent(
        "start",
        arguments,
        principal=_principal(),
        request_id=SECOND_REQUEST,
    )
    assert replay["ok"], replay
    assert replay["data"]["request_replayed"] is True

    conn = _connect(service)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        row = conn.execute(
            "SELECT status,operation_id FROM planning_intent_challenges"
        ).fetchone()
        assert tuple(row) == ("consumed", replay["submission_id"])
    finally:
        conn.close()


@pytest.mark.flake_stress
@pytest.mark.quarantined(
    issue="DISH-flake-038-concurrent-challenge-backup-race",
    owner="Marco",
    first_seen="2026-07-31",
    quarantined_on="2026-07-31",
    expires="2026-08-07",
    signature="sqlite3.DatabaseError: legacy backup schema version mismatch racing "
    "concurrent initialize_database calls in _backup_legacy_database",
)
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

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
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


@pytest.mark.flake_stress
def test_concurrent_exact_followups_converge_on_one_operation(tmp_path):
    entered_read = threading.Event()
    release_read = threading.Event()
    read_lock = threading.Lock()

    class BlockingBackend(Backend):
        def __init__(self):
            super().__init__()
            self._blocked_once = False

        def read_task(self, gid):
            with read_lock:
                should_block = not self._blocked_once
                if should_block:
                    self._blocked_once = True
            if should_block:
                entered_read.set()
                assert release_read.wait(timeout=5)
            return super().read_task(gid)

    backend = BlockingBackend()
    service, _ = _service(tmp_path, backend=backend)
    challenge = _issue(service)
    arguments = _planning_arguments(
        intent_challenge_id=challenge["data"]["intent_challenge_id"],
        intent_basis="user_requested",
    )
    results: list[dict] = []
    failures: list[BaseException] = []
    second_started = threading.Event()
    second_lock_attempted = threading.Event()
    second_done = threading.Event()
    challenge_id = arguments["intent_challenge_id"]
    lock_index = sum(challenge_id.encode("utf-8")) % len(
        service._planning_intent_locks
    )
    real_lock = service._planning_intent_locks[lock_index]

    class ObservedPlanningIntentLock:
        def __enter__(self):
            if threading.current_thread().name == "planning-followup-second":
                second_lock_attempted.set()
            real_lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            real_lock.release()

    locks = list(service._planning_intent_locks)
    locks[lock_index] = ObservedPlanningIntentLock()
    service._planning_intent_locks = tuple(locks)

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

    first = threading.Thread(target=invoke, name="planning-followup-first")
    first.start()
    assert entered_read.wait(timeout=5)
    second = threading.Thread(
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
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive()
    assert not second.is_alive()

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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
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
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

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
