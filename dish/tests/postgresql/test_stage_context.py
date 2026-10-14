"""Successful stage starts carry current Honest-main protocol text."""
from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from dish_pg import stage_context as stage_context_module
from dish_pg.database import session_scope
from dish_pg.stage_context import (
    STAGE_CONTEXT_GUIDANCE,
    StageContextUnavailable,
    build_stage_context,
)
from dish_pg.transition import ProjectionService
from dish_service.action_guidance import attach_connected_agent_guidance
from dish_service.http import DishHTTPServer
from tests.support.postgresql.runtime_validation import _post_json, runtime_service
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.thread_teardown import start_server_thread, stop_server


ROLE_PATHS = {
    "planning": "dish-planning-protocol.md",
    "research": "dish-research-protocol.md",
    "verification": "dish-verification-protocol.md",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def honest_repo(tmp_path: Path) -> tuple[Path, str, str]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    root = tmp_path / "honest"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "test")
    for path in (*ROLE_PATHS.values(), "CLAUDE.md"):
        (seed / path).write_text(f"# OLD {path}\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "old")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    old = _git(seed, "rev-parse", "HEAD")
    subprocess.run(["git", "clone", str(origin), str(root)], check=True, capture_output=True)
    for path in (*ROLE_PATHS.values(), "CLAUDE.md"):
        (seed / path).write_text(f"# CURRENT MAIN {path}\n", encoding="utf-8")
    _git(seed, "commit", "-am", "current")
    _git(seed, "push", "origin", "main")
    current = _git(seed, "rev-parse", "HEAD")
    assert _git(root, "rev-parse", "HEAD") == old
    return root, old, current


def test_build_stage_context_pulls_and_serves_current_main(honest_repo) -> None:
    root, old, current = honest_repo
    block = build_stage_context(role="research", honest_root=root)
    assert block["available"] is True
    assert block["source"] == {
        "repository": "marcogallotta/honest-pantry",
        "ref": "refs/heads/main",
        "honest_commit": current,
    }
    protocol, instructions = block["files"]
    assert protocol["path"] == "dish-research-protocol.md"
    assert protocol["git_commit"] == current
    assert protocol["text"] == "# CURRENT MAIN dish-research-protocol.md\n"
    assert instructions["text"] == "# CURRENT MAIN CLAUDE.md\n"
    assert old not in {protocol["git_commit"], instructions["git_commit"]}
    assert _git(root, "rev-parse", "HEAD") == current


def test_research_start_returns_current_main_context(
    workflow_db, honest_repo, tmp_path: Path
) -> None:
    factory, ids, context, task_id = workflow_db
    root, _old, current = honest_repo
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner="gpt-action",
            agent="gpt",
        )
        ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="stage context test authority",
            created_at=NOW,
            external_effects_enabled=True,
        )
    service = runtime_service(factory, tmp_path)
    service.config = replace(
        service.config,
        honest_root=root,
        action_token="postgres-action-token",
    )
    body = {
        "client": {"run_id": str(run_id), "request_id": str(_next(ids))},
        "arguments": {"dish_id": str(task_id), "kind": "initial", "agent": "gpt"},
    }
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="action") as server:
        thread = start_server_thread(server, name="stage-context-action-http")
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/action/start"
        try:
            status, result = _post_json(url, token="postgres-action-token", body=body)
        finally:
            stop_server(server, thread)
    assert status == 200, result
    block = result["data"]["stage_context"]
    assert block["source"]["honest_commit"] == current
    assert block["files"][0]["text"] == "# CURRENT MAIN dish-research-protocol.md\n"
    assert any(
        STAGE_CONTEXT_GUIDANCE in line
        for line in result["data"]["agent_guidance"]["instructions"]
    )


def test_stage_context_failure_degrades_to_read_guidance(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    bad_root = tmp_path / "not-a-repo"
    bad_root.mkdir()
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
    service = runtime_service(factory, tmp_path)
    service.config = replace(service.config, honest_root=bad_root)
    result = service.execute_agent(
        "start",
        {"task_id": str(task_id), "kind": "initial", "agent": "claude"},
        principal=__import__(
            "dish_service.leases", fromlist=["ServicePrincipal"]
        ).ServicePrincipal.from_values("owner-1", str(run_id)),
        request_id=str(_next(ids)),
    )
    assert result["ok"] is True
    block = result["data"]["stage_context"]
    assert block["available"] is False
    guided = attach_connected_agent_guidance(
        {
            "ok": True,
            "command": "start",
            "allowed_actions": [],
            "data": dict(result["data"]),
        }
    )
    lines = guided["data"]["agent_guidance"]["instructions"]
    assert not any(STAGE_CONTEXT_GUIDANCE in line for line in lines)
    assert any("dish_honest_read" in line for line in lines)


def test_non_main_checkout_is_refused(honest_repo) -> None:
    root, _old, _current = honest_repo
    _git(root, "checkout", "-b", "other")
    with pytest.raises(StageContextUnavailable, match="not on main"):
        build_stage_context(role="research", honest_root=root)


def test_budget_omits_claude_md_before_protocol(honest_repo) -> None:
    root, _old, _current = honest_repo
    protocol_bytes = len("# CURRENT MAIN dish-research-protocol.md\n".encode())
    block = build_stage_context(
        role="research",
        honest_root=root,
        max_text_bytes=protocol_bytes,
    )
    assert [item["path"] for item in block["files"]] == [
        "dish-research-protocol.md"
    ]
    assert block["omitted"][0]["path"] == "CLAUDE.md"
    with pytest.raises(StageContextUnavailable, match="budget"):
        build_stage_context(
            role="research",
            honest_root=root,
            max_text_bytes=protocol_bytes - 1,
        )


def test_default_budget_covers_current_protocol_sizes() -> None:
    assert stage_context_module.STAGE_CONTEXT_MAX_TEXT_BYTES >= 100 * 1024
