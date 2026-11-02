from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from dish_tool import prod_release as release
from dish_tool import release_hot as hot

from tests.test_prod_release import _write_release


@pytest.fixture
def system(tmp_path, monkeypatch):
    old = _write_release(tmp_path / "releases", "a" * 40)
    new = _write_release(tmp_path / "releases", "b" * 40)
    older = _write_release(tmp_path / "releases", "c" * 40)
    for candidate in (old, new, older):
        p = candidate.root / release.MANIFEST_NAME
        payload = json.loads(p.read_text())
        payload["hot_deploy_contract"] = hot.CONTRACT
        p.write_text(json.dumps(payload))
    env = tmp_path / "service.env"
    env.write_text("DISH_PG_EXPECTED_SCHEMA_HEAD=0054_test\n")
    values = {
        "current": str(tmp_path / "current"),
        "previous": str(tmp_path / "previous"),
        "release_root": str(tmp_path / "releases"),
        "state_root": str(tmp_path / "operations"),
        "env_files": [str(env)],
        "mcp_env_file": str(env),
        "backend_unit": "backend",
        "mcp_unit": "mcp",
        "timeout": 1,
        "recovery_timeout": 1,
    }
    target = hot.Target("test", values)
    target.path("current").symlink_to(old.root)
    target.path("previous").symlink_to(older.root)
    events = []
    monkeypatch.setattr(
        hot, "unit_bindings", lambda *a: {"backend": "fixed", "mcp": "fixed"}
    )
    monkeypatch.setattr(hot, "unit_values", lambda *a: {"MainPID": "0"})
    monkeypatch.setattr(hot, "authenticated_read", lambda *a, **k: "token")
    monkeypatch.setattr(
        hot, "database_check", lambda t, c, b: events.append(("db", c.source_commit))
    )
    monkeypatch.setattr(
        hot, "run", lambda command, *a, **k: events.append(("restart", command[-1]))
    )
    monkeypatch.setattr(
        hot,
        "verify_pair",
        lambda t, c, b: (
            events.append(("verify", c.source_commit))
            or {
                "backend": {"ok": True, "code_release": c.source_commit},
                "mcp": {"ok": True, "code_release": c.source_commit},
            }
        ),
    )
    hot.atomic_json(
        hot.pair_path(target, new, old),
        {"binding": hot.pair_binding(target, new, old), "mixed_pairs": [True, True]},
    )
    hot.atomic_json(
        hot.pair_path(target, old, old),
        {"binding": hot.pair_binding(target, old, old), "mixed_pairs": [True, True]},
    )
    return target, old, new, older, events


class Crash(BaseException):
    pass


@pytest.mark.parametrize(
    "phase",
    [
        "admitted",
        "switch-pointers",
        "pointers-set",
        "restart-backend",
        "backend-restarted",
        "restart-mcp",
        "mcp-restarted",
        "candidate-verified",
    ],
)
def test_every_interruption_blocks_new_operation_and_recovers_exact_prior(
    system, monkeypatch, phase
):
    target, old, new, older, events = system
    original = hot.checkpoint

    def crash(t, record, current):
        original(t, record, current)
        if current == phase:
            raise Crash()

    monkeypatch.setattr(hot, "checkpoint", crash)
    with pytest.raises(Crash):
        hot.activate(target, new)
    pending = target.pending.read_bytes()
    monkeypatch.setattr(hot, "target_config", lambda profile: target)
    monkeypatch.setattr(
        hot,
        "resolve_latest",
        lambda *a: pytest.fail("resolved latest before pending admission"),
    )
    for command in ("deploy", "activate", "rollback"):
        with pytest.raises(hot.OperationError, match="unresolved"):
            hot.execute(
                argparse.Namespace(
                    profile="test",
                    command=command,
                    target="latest",
                    release=new.source_commit,
                )
            )
        assert target.pending.read_bytes() == pending
    monkeypatch.setattr(hot, "checkpoint", original)
    hot.recover(target)
    assert target.path("current").resolve() == old.root
    assert target.path("previous").resolve() == older.root
    assert not target.pending.exists()
    count = len([e for e in events if e[0] == "restart"])
    assert hot.recover(target)["state"] == "ALREADY_TERMINAL"
    assert len([e for e in events if e[0] == "restart"]) == count


def test_terminal_success_before_pending_removal_does_not_replay(system, monkeypatch):
    target, _old, new, _older, events = system
    original = hot.terminal

    def crash(t, record, state, components):
        retained = t.pending.read_bytes()
        original(t, record, state, components)
        t.pending.write_bytes(retained)
        raise Crash()

    monkeypatch.setattr(hot, "terminal", crash)
    with pytest.raises(Crash):
        hot.activate(target, new)
    count = len([e for e in events if e[0] == "restart"])
    monkeypatch.setattr(hot, "terminal", original)
    assert hot.recover(target)["state"] == "ALREADY_TERMINAL"
    assert target.path("current").resolve() == new.root
    assert len([e for e in events if e[0] == "restart"]) == count


@pytest.mark.parametrize("change", ["pointer", "config", "units"])
def test_recovery_rejects_external_drift_without_restarts(system, monkeypatch, change):
    target, _old, new, older, events = system
    original = hot.checkpoint

    def crash(t, record, phase):
        original(t, record, phase)
        raise Crash()

    monkeypatch.setattr(hot, "checkpoint", crash)
    with pytest.raises(Crash):
        hot.activate(target, new)
    monkeypatch.setattr(hot, "checkpoint", original)
    if change == "pointer":
        release._set_pointer(target.path("current"), str(older.root))
    elif change == "config":
        Path(target.values["mcp_env_file"]).write_text("CHANGED=1\n")
    else:
        monkeypatch.setattr(hot, "unit_bindings", lambda *a: {"changed": True})
    with pytest.raises(hot.OperationError, match="outside recorded") as exc:
        hot.recover(target)
    assert exc.value.code == "EXTERNAL_STATE_CHANGED"
    assert not [e for e in events if e[0] == "restart"]
    assert target.pending.exists()


def test_backend_success_mcp_failure_recovers_both_and_returns_nonzero(
    system, monkeypatch
):
    target, old, new, older, events = system
    failed = False

    def restart(command, *a, **k):
        nonlocal failed
        events.append(("restart", command[-1]))
        if command[-1] == "mcp" and not failed:
            failed = True
            raise hot.OperationError("RESTART_FAILED", "MCP restart failed")

    monkeypatch.setattr(hot, "run", restart)
    with pytest.raises(hot.OperationError) as exc:
        hot.activate(target, new)
    assert exc.value.exit_code == 3
    assert exc.value.code == "RECOVERED_TO_PRIOR"
    assert target.path("current").resolve() == old.root
    assert target.path("previous").resolve() == older.root
    assert [e[1] for e in events if e[0] == "restart"] == [
        "backend",
        "mcp",
        "backend",
        "mcp",
    ]


def test_failed_recovery_keeps_pending_for_explicit_recover(system, monkeypatch):
    target, _old, new, _older, _events = system
    monkeypatch.setattr(
        hot,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            hot.OperationError("RESTART_FAILED", "failed")
        ),
    )
    with pytest.raises(hot.OperationError) as exc:
        hot.activate(target, new)
    assert exc.value.exit_code == 4
    assert target.pending.exists()


def test_same_target_still_restarts_and_verifies_both(system):
    target, old, _new, older, events = system
    hot.activate(target, old)
    assert [e for e in events if e[0] == "restart"] == [
        ("restart", "backend"),
        ("restart", "mcp"),
    ]
    assert target.path("previous").resolve() == older.root


def test_missing_or_stale_pair_evidence_never_restarts(system):
    target, old, new, _older, events = system
    hot.pair_path(target, new, old).unlink()
    with pytest.raises(hot.OperationError, match="evidence missing"):
        hot.activate(target, new)
    assert not [e for e in events if e[0] == "restart"]
    assert not target.pending.exists()


def test_token_failure_never_changes_pointers_or_creates_pending(system, monkeypatch):
    target, _old, new, _older, events = system
    before = hot.pointer_states(target)
    monkeypatch.setattr(
        hot,
        "authenticated_read",
        lambda *a, **k: (_ for _ in ()).throw(
            hot.OperationError("AUTH_REQUIRED", "expired token")
        ),
    )
    with pytest.raises(hot.OperationError, match="expired"):
        hot.activate(target, new)
    assert hot.pointer_states(target) == before
    assert not target.pending.exists()
    assert not [e for e in events if e[0] == "restart"]


def test_schema_incompatible_previous_never_stops_services(system, monkeypatch):
    target, _old, _new, older, events = system

    def incompatible(t, candidate, budget):
        if candidate.source_commit == older.source_commit:
            raise hot.OperationError("SCHEMA_INCOMPATIBLE", "schema incompatible")

    monkeypatch.setattr(hot, "database_check", incompatible)
    with pytest.raises(hot.OperationError, match="schema incompatible"):
        hot.activate(target, older)
    assert not [e for e in events if e[0] == "restart"]


def test_status_stale_mcp_is_not_green_and_bad_previous_is_nonblocking(
    system, monkeypatch
):
    target, old, new, _older, _events = system

    def observe(t, component, selected, budget):
        return {
            "ok": component != "mcp",
            "state": "VERSION_MISMATCH",
            "code_release": new.source_commit
            if component == "mcp"
            else old.source_commit,
        }

    monkeypatch.setattr(hot, "observe_component", observe)
    result = hot.snapshot(target)
    assert not result["ok"]
    monkeypatch.setattr(
        hot,
        "observe_component",
        lambda *a: {"ok": True, "code_release": old.source_commit},
    )
    result = hot.snapshot(target)
    assert result["ok"] and not result["rollback_available"]


def test_invalid_explicit_python_fails_even_for_staged_release(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "_exact_commit", lambda *a: "a" * 40)
    (tmp_path / ("a" * 40)).mkdir()
    with pytest.raises(release.ReleaseError, match="interpreter unavailable"):
        release.stage_release(tmp_path, "a" * 40, tmp_path, tmp_path / "invalid-python")


@pytest.mark.parametrize(
    "metadata_url,resource,success",
    [
        (
            "http://127.0.0.1:32001/.well-known/oauth-protected-resource/mcp",
            "http://127.0.0.1:32001/mcp",
            True,
        ),
        (
            "http://127.0.0.1:32002/.well-known/oauth-protected-resource/mcp",
            "http://127.0.0.1:32001/mcp",
            False,
        ),
        (
            "http://127.0.0.1:32001/.well-known/oauth-protected-resource/mcp",
            "http://127.0.0.1:32002/mcp",
            False,
        ),
    ],
)
def test_discovery_binds_exact_resource_origin(
    monkeypatch, metadata_url, resource, success
):
    import urllib.error
    from email.message import Message

    headers = Message()
    headers["WWW-Authenticate"] = 'Bearer resource_metadata="' + metadata_url + '"'

    def unauthorized(*args, **kwargs):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:32001/mcp", 401, "auth", headers, None
        )

    monkeypatch.setattr(hot.urllib.request, "urlopen", unauthorized)
    monkeypatch.setattr(hot, "request_json", lambda *a, **k: {"resource": resource})
    target = hot.Target("test", {"mcp_url": "http://127.0.0.1:32001/mcp"})
    if success:
        hot.resource_identity(target, hot.Budget(1))
    else:
        with pytest.raises(hot.OperationError, match="metadata"):
            hot.resource_identity(target, hot.Budget(1))


def test_warm_deploy_validates_explicit_interpreter_before_effects(
    system, monkeypatch, tmp_path
):
    target, old, new, older, events = system
    target.values["repository"] = str(tmp_path)
    monkeypatch.setattr(hot, "target_config", lambda *a: target)
    monkeypatch.setattr(hot, "resolve_latest", lambda *a: new.source_commit)
    monkeypatch.setattr(release, "_exact_commit", lambda *a: new.source_commit)
    args = release._parser().parse_args(
        ["--profile", "test", "deploy", "latest", "--python", str(tmp_path / "absent")]
    )
    with pytest.raises(release.ReleaseError, match="interpreter unavailable"):
        hot.execute(args)
    assert not events and not target.pending.exists()


def test_recorded_old_process_launcher_remains_valid_after_pointer_switch(
    system, monkeypatch, tmp_path
):
    target, old, new, older, events = system
    target.values.update(
        isolation_root=str(tmp_path), health_url="http://127.0.0.1:32001/health"
    )
    binary = old.dish_root / ".venv/bin/python"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.symlink_to("/usr/bin/python3")
    launcher = str(target.path("current") / "dish/.venv/bin/python")
    release._set_pointer(target.path("current"), str(new.root))
    witness = {
        "pid": 123,
        "start_ticks": "456",
        "cwd": str(old.dish_root),
        "executable": str(binary.resolve()),
        "arguments": launcher + " -m dish_service",
    }
    monkeypatch.setattr(
        hot, "unit_values", lambda *a: {"MainPID": "123", "ActiveState": "active"}
    )
    monkeypatch.setattr(hot, "process_witness", lambda *a: witness)
    monkeypatch.setattr(hot, "owns_listener", lambda *a: True)
    monkeypatch.setattr(
        hot,
        "request_json",
        lambda *a: {
            "ok": True,
            "code_release": old.source_commit,
            "pid": 123,
            "start_ticks": "456",
        },
    )
    assert hot.observe_component(target, "backend", old, hot.Budget(1))["ok"]
