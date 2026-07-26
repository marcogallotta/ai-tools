"""`raw` escape hatch (asana-cli-fixes.md P1 #6).

`raw` must stay a genuine, unrestricted escape hatch (no method allowlist),
and must dispatch through the SDK's ApiClient.call_api() rather than the
separate urllib transport, wrapping any body in {"data": ...} itself (since
call_api() does not do that automatically). None of the SDK dispatch is
implemented yet -- c_raw still uses the old req()/urllib path -- so the
dispatch assertions here are expected to fail until P1 #6 lands.
"""
import io
import json
import urllib.request

import asana
import pytest


def _run_raw(cli, monkeypatch, method, path, stdin_body=None, calls=None):
    calls = calls if calls is not None else []

    def fake_call_api(self, resource_path, http_method, *a, **kw):
        calls.append({"resource_path": resource_path, "method": http_method, "args": a, "kwargs": kw})
        return {}

    monkeypatch.setattr(asana.ApiClient, "call_api", fake_call_api)

    # Guard against the old urllib transport actually hitting the network
    # if c_raw hasn't been migrated to call_api() yet (red test).
    def no_network(*a, **kw):
        raise AssertionError("raw hit the network via urllib instead of ApiClient.call_api()")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)

    stdin = io.StringIO(json.dumps(stdin_body) if stdin_body is not None else "")
    stdin.isatty = lambda: stdin_body is None
    monkeypatch.setattr("sys.stdin", stdin)

    cli.c_raw(method, path)
    return calls


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_method_accepted_no_allowlist_rejection(cli, monkeypatch, method):
    calls = _run_raw(cli, monkeypatch, method, "/tasks/123", stdin_body={"name": "x"} if method != "GET" else None)
    assert len(calls) == 1
    assert calls[0]["method"] == method


def test_dispatches_via_sdk_call_api_not_urllib(cli, monkeypatch):
    calls = _run_raw(cli, monkeypatch, "GET", "/tasks/123")
    assert len(calls) == 1
    assert calls[0]["resource_path"] == "/tasks/123"


def test_put_wraps_body_in_data_envelope(cli, monkeypatch):
    calls = _run_raw(cli, monkeypatch, "PUT", "/tasks/123", stdin_body={"name": "renamed"})
    call = calls[0]
    body = call["args"][-1] if len(call["args"]) >= 3 else call["kwargs"].get("body")
    assert body == {"data": {"name": "renamed"}}


def test_delete_requires_no_confirmation_flag(cli, monkeypatch):
    """DELETE must work with no extra flag beyond the Bash-tool permission
    hook -- there is no in-CLI confirmation gate to satisfy."""
    calls = _run_raw(cli, monkeypatch, "DELETE", "/tasks/123")
    assert len(calls) == 1
    assert calls[0]["method"] == "DELETE"
