from __future__ import annotations

import json

from dish_tool import admin_cli
from tests.test_dish_admin_expire_lease import ADMIN_RUN, EXPIRY_REQUEST, TASK_GID


class _RecordingAdminClient:
    run_id = ADMIN_RUN

    def __init__(self):
        self.calls = []

    def expire_lease(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "ok": True,
            "command": "expire-lease",
            "code": "OK",
            "task_gid": kwargs["task_gid"],
            "submission_id": None,
            "state": None,
            "retryable": False,
            "allowed_actions": [],
            "data": {"outcome": "no_active_lease", "request_id": kwargs["request_id"]},
            "errors": [],
        }


def test_cli_prints_and_flushes_replay_identity_before_dispatch(capsys):
    client = _RecordingAdminClient()
    status = admin_cli.main(
        [
            "expire-lease",
            "https://app.asana.com/0/987654321/123456789",
            "--reason",
            " agent died ",
            "--request-id",
            EXPIRY_REQUEST,
        ],
        application=client,
    )
    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == (
        f"expire-lease request_id={EXPIRY_REQUEST} run_id={ADMIN_RUN}\n"
    )
    assert client.calls == [
        {
            "lease_id": None,
            "task_gid": TASK_GID,
            "reason": "agent died",
            "request_id": EXPIRY_REQUEST,
        }
    ]


def test_cli_local_mode_rejects_before_local_dependencies(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DISH_MODE", "local")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", ADMIN_RUN)
    monkeypatch.setattr(
        admin_cli,
        "initialize_database",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("database opened")),
    )
    monkeypatch.setattr(
        admin_cli,
        "AsanaBackend",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("backend created")),
    )
    status = admin_cli.main(
        ["expire-lease", TASK_GID, "--reason", "owner dead"]
    )
    result = json.loads(capsys.readouterr().out)
    assert status != 0
    assert result["errors"][0]["rule"] == "shared_service_required"


def test_cli_malformed_target_and_invalid_run_id_fail_before_dependencies(
    monkeypatch, capsys
):
    def bomb(*_args, **_kwargs):
        raise AssertionError("local workflow dependency constructed")

    monkeypatch.setattr(admin_cli, "initialize_database", bomb)
    monkeypatch.setattr(admin_cli, "AsanaBackend", bomb)

    status = admin_cli.main(["expire-lease", "not-a-target", "--reason", "x"])
    malformed = json.loads(capsys.readouterr().out)
    assert status != 0
    assert malformed["errors"][0]["rule"] == "uuid_identifier_required"

    monkeypatch.setenv("DISH_MODE", "service")
    monkeypatch.setenv("DISH_SERVICE_URL", "http://dish.invalid")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", "bad")
    monkeypatch.setenv("DISH_ADMIN_TOKEN", "admin-secret")
    status = admin_cli.main(["expire-lease", TASK_GID, "--reason", "x"])
    invalid_run = json.loads(capsys.readouterr().out)
    assert status != 0
    assert invalid_run["errors"][0]["rule"] == "uuid_identifier_required"
