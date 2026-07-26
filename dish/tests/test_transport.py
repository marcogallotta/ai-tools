"""SDK client wiring and error mapping (asana-cli-fixes.md P0 #1/#2)."""
import asana
from asana.rest import ApiException
import pytest


def _api_exc(status, reason="", body=b""):
    e = ApiException(status=status, reason=reason)
    e.body = body
    return e


def test_client_constructed_once_and_reused(cli, monkeypatch):
    calls = []
    real_init = asana.ApiClient.__init__

    def counting_init(self, *a, **kw):
        calls.append((a, kw))
        return real_init(self, *a, **kw)

    monkeypatch.setattr(asana.ApiClient, "__init__", counting_init)

    first = cli.client()
    second = cli.client()

    assert len(calls) == 1
    assert first is second


def test_client_reused_across_commands_in_one_invocation(cli, monkeypatch):
    """A single CLI invocation must not reconstruct the client per command."""
    seen_clients = []
    orig_tasks_api = asana.TasksApi

    class TrackingTasksApi(orig_tasks_api):
        def __init__(self, api_client):
            seen_clients.append(api_client)
            super().__init__(api_client)

    monkeypatch.setattr(asana, "TasksApi", TrackingTasksApi)
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: {"data": {"name": "n", "notes": ""}},
    )

    cli.c_get("1")
    cli.c_get("2")

    assert len(seen_clients) == 2
    assert seen_clients[0] is seen_clients[1]


def test_401_surfaces_as_auth_error_not_traceback(cli, monkeypatch):
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: (_ for _ in ()).throw(_api_exc(401, "Unauthorized")),
    )

    with pytest.raises(SystemExit) as exc:
        cli.c_get("123")

    assert "401" in str(exc.value)
    assert "auth" in str(exc.value).lower()


def test_404_names_the_resource(cli, monkeypatch):
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: (_ for _ in ()).throw(_api_exc(404, "Not Found")),
    )

    with pytest.raises(SystemExit) as exc:
        cli.c_get("999888777")

    msg = str(exc.value)
    assert "404" in msg
    assert "999888777" in msg


def test_429_surfaces_as_rate_limit_message(cli, monkeypatch):
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: (_ for _ in ()).throw(_api_exc(429, "Too Many Requests")),
    )

    with pytest.raises(SystemExit) as exc:
        cli.c_get("123")

    msg = str(exc.value).lower()
    assert "429" in msg
    assert "rate limit" in msg


def test_5xx_surfaces_as_server_error_not_swallowed(cli, monkeypatch):
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: (_ for _ in ()).throw(_api_exc(503, "Service Unavailable")),
    )

    with pytest.raises(SystemExit) as exc:
        cli.c_get("123")

    msg = str(exc.value)
    assert "503" in msg
    assert "server error" in msg.lower()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429, 500, 503])
def test_any_api_exception_sets_nonzero_exit(cli, monkeypatch, status):
    monkeypatch.setattr(
        asana.TasksApi, "get_task",
        lambda self, gid, opts, **kw: (_ for _ in ()).throw(_api_exc(status)),
    )

    with pytest.raises(SystemExit) as exc:
        cli.c_get("123")

    code = exc.value.code
    assert code not in (0, None, "")
