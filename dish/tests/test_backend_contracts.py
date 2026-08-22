import threading
from typing import Any

import pytest

from dish_tool.backend import (
    AsanaBackend,
    close_asana_sdk_client,
    load_asana_pat,
    map_backend_exception,
)
from dish_tool.constants import (
    ASANA_REQUEST_TIMEOUT,
    CONNECT_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
)
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.models import RequestPhase


@pytest.mark.smoke
def test_backend_failure_classification_tracks_request_phase():
    pre_send = map_backend_exception(
        TimeoutError("connect failed"), phase=RequestPhase.PRE_SEND
    )
    assert pre_send.code == "BACKEND_REJECTED"
    assert pre_send.retryable is True

    possibly_sent = map_backend_exception(
        TimeoutError("response lost"), phase=RequestPhase.POSSIBLY_SENT
    )
    assert possibly_sent.code == "BACKEND_UNCERTAIN"
    assert possibly_sent.retryable is False

    class ServerError(Exception):
        status = 503
        body = "unavailable"
        reason = "Service Unavailable"

    server = map_backend_exception(ServerError(), phase=RequestPhase.RESPONSE_RECEIVED)
    assert server.code == "BACKEND_UNCERTAIN"
    assert server.status == 503

    class RequestTimeout(Exception):
        status = 408
        body = "request timeout"
        reason = "Request Timeout"

    timeout_response = map_backend_exception(
        RequestTimeout(), phase=RequestPhase.RESPONSE_RECEIVED
    )
    assert timeout_response.code == "BACKEND_UNCERTAIN"


@pytest.mark.smoke
def test_backend_call_without_explicit_tracker_marks_request_as_sent():
    backend = AsanaBackend(api_client=object())

    def fail_after_send(*args, **kwargs):
        raise TimeoutError("response lost")

    with pytest.raises(BackendFailure) as exc:
        backend.call(fail_after_send)
    assert exc.value.code == "BACKEND_UNCERTAIN"
    assert exc.value.phase == RequestPhase.POSSIBLY_SENT.value


@pytest.mark.smoke
def test_backend_call_invokes_sdk_without_async_request():
    """close_asana_sdk_client's bounded pool shutdown is only safe because the
    Asana SDK's worker pool never carries a live request; it stays safe only
    as long as nothing here passes ``async_req=True``."""
    backend = AsanaBackend(api_client=object())
    recorded_kwargs: dict[str, Any] = {}

    def record(*args, **kwargs):
        recorded_kwargs.update(kwargs)
        return {"data": {}}

    backend.call(record)

    assert "async_req" not in recorded_kwargs or not recorded_kwargs["async_req"]


@pytest.mark.smoke
def test_asana_backend_reuses_client_and_disables_sdk_retries(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "test-token")
    backend = AsanaBackend()
    try:
        first = backend.client()
        second = backend.client()

        assert first is second
        assert first.configuration.return_page_iterator is False
        assert first.configuration.retry_strategy.total == 0
    finally:
        backend.close()


@pytest.mark.smoke
def test_asana_backend_closes_only_the_client_it_created(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "test-token")
    owned = AsanaBackend()
    owned_client = owned.client()
    owned_pool = owned_client.pool

    owned.close()
    owned.close()

    assert owned_pool._state == "CLOSE"
    assert not owned_pool._terminate.still_active()
    with pytest.raises(DishRuleError) as exc:
        owned.client()
    assert exc.value.rule == "asana_backend_closed"

    class InjectedClient:
        def __init__(self):
            self.pool = type("Pool", (), {"close": lambda self: (_ for _ in ()).throw(AssertionError), "join": lambda self: (_ for _ in ()).throw(AssertionError)})()

    injected = InjectedClient()
    backend = AsanaBackend(api_client=injected)
    backend.close()
    assert injected.pool is not None


@pytest.mark.smoke
def test_close_asana_sdk_client_terminates_a_pool_that_will_not_join(monkeypatch):
    from dish_tool import backend as backend_module

    monkeypatch.setattr(backend_module, "POOL_SHUTDOWN_JOIN_SECONDS", 0.05)
    release = threading.Event()

    class StuckPool:
        def __init__(self):
            self.closed = False
            self.terminated = False
            self._terminate = type("Finalizer", (), {"still_active": lambda self: False})()

        def close(self):
            self.closed = True

        def join(self):
            release.wait()

        def terminate(self):
            self.terminated = True
            release.set()

    pool = StuckPool()

    class Client:
        def __init__(self):
            self.pool = pool

    api_client = Client()

    close_asana_sdk_client(api_client)

    assert pool.closed is True
    assert pool.terminated is True


@pytest.mark.smoke
def test_asana_auth_loader_and_timeout_configuration(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ASANA_PAT=file-token\n")
    monkeypatch.delenv("ASANA_PAT", raising=False)
    monkeypatch.setenv("ASANA_ENV", str(env_file))
    assert load_asana_pat() == "file-token"

    monkeypatch.setenv("ASANA_PAT", "env-token")
    assert load_asana_pat() == "env-token"
    assert ASANA_REQUEST_TIMEOUT == (
        CONNECT_TIMEOUT_SECONDS,
        READ_TIMEOUT_SECONDS,
    )


@pytest.mark.smoke
def test_project_task_listing_requests_placement_fields_and_returns_cursor(monkeypatch):
    import asana

    captured: dict[str, Any] = {}

    class TasksApi:
        def __init__(self, client):
            captured["client"] = client

        def get_tasks_for_project(self, project_gid, opts, **kwargs):
            captured["project_gid"] = project_gid
            captured["opts"] = dict(opts)
            captured["kwargs"] = dict(kwargs)
            return {
                "data": [
                    {
                        "gid": "1001",
                        "name": "Dish",
                        "completed": False,
                        "projects": [{"gid": project_gid}],
                        "memberships": [
                            {
                                "project": {"gid": project_gid},
                                "section": {"gid": "rq", "name": "Research Queue"},
                            }
                        ],
                    }
                ],
                "next_page": {"offset": "next-page"},
            }

    monkeypatch.setattr(asana, "TasksApi", TasksApi)
    backend = AsanaBackend(api_client=object())

    tasks, cursor = backend.list_tasks_for_project("cooking-project", cursor="page-1")

    assert tasks[0]["gid"] == "1001"
    assert cursor == "next-page"
    assert captured["project_gid"] == "cooking-project"
    assert captured["opts"]["offset"] == "page-1"
    assert captured["opts"]["limit"] == 100
    assert "memberships.project.gid" in captured["opts"]["opt_fields"]
    assert "memberships.section.gid" in captured["opts"]["opt_fields"]
    assert "memberships.section.name" in captured["opts"]["opt_fields"]
    assert captured["kwargs"]["_request_timeout"] == ASANA_REQUEST_TIMEOUT
