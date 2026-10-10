"""A successful stage ``start`` carries the protocol text Dish froze for the run."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from dish_pg import models
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
from dish_service.leases import ServicePrincipal
from tests.postgresql.test_postgres_runtime_validation_http import _post_json
from tests.support.postgresql.command import (
    _add_verification_queue,
    _port,
    _prepare_for_verification,
    _start_initial,
)
from tests.support.postgresql.runtime_validation import runtime_service
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db  # noqa: F401
from tests.support.thread_teardown import start_server_thread, stop_server

FROZEN = {
    "dish-planning-protocol.md": "# Planning protocol (frozen)\n",
    "dish-research-protocol.md": "# Research protocol (frozen)\nResearch rule one.\n",
    "dish-verification-protocol.md": "# Verification protocol (frozen)\n",
    "dish-cooking-protocol.md": "# Cooking protocol (frozen)\n",
    "CLAUDE.md": "# Honest CLAUDE.md (frozen)\n",
}
ROLE_PATHS = {
    "planning": "dish-planning-protocol.md",
    "research": "dish-research-protocol.md",
    "verification": "dish-verification-protocol.md",
    "cooking": "dish-cooking-protocol.md",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture()
def honest_repo(tmp_path: Path) -> tuple[Path, str]:
    """An Honest checkout whose current tip has moved past the frozen commit."""

    root = tmp_path / "honest"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "test")
    for name, text in FROZEN.items():
        (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "frozen")
    frozen = _git(root, "rev-parse", "HEAD")
    for name in FROZEN:
        (root / name).write_text(f"# CURRENT MAIN {name}\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "moved on")
    return root, frozen


def _source_ids(commit: str) -> dict[str, object]:
    return {
        "repo": "honest-pantry",
        "commit": commit,
        "protocol_files": {
            role: {"path": path, "sha256": _sha(FROZEN[path])}
            for role, path in ROLE_PATHS.items()
        },
    }


def _freeze_binding(factory, context, commit: str) -> None:
    """Give the fixture binding a real bootstrap-shaped source identity.

    Bindings are immutable authority rows, so the fixture's SQLite update guard
    is lifted only for this one setup write and then reinstalled.
    """

    with session_scope(factory) as session:
        guards = session.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='honest_contract_bindings' AND sql LIKE '%BEFORE UPDATE%'"
        )).all()
        for name, _sql in guards:
            session.execute(text(f'DROP TRIGGER "{name}"'))
        binding = session.get(models.HonestContractBinding, context["binding_id"])
        binding.source_ids = _source_ids(commit)
        session.flush()
        for _name, sql in guards:
            session.execute(text(sql))


def _binding(commit: str, **source_overrides) -> models.HonestContractBinding:
    return models.HonestContractBinding(
        binding_id=__import__("uuid").uuid4(),
        binding_kind="release",
        source_identity="honest@x",
        dish_release="dish-x",
        honest_release="honest-x",
        protocol_release="protocol-1",
        protocol_sha256="a" * 64,
        schema_release="2",
        schema_sha256="b" * 64,
        source_ids={**_source_ids(commit), **source_overrides},
        provenance={},
        resolved_at=NOW,
    )


def test_research_start_returns_frozen_protocol_not_current_main_and_replays(
    workflow_db, honest_repo, tmp_path: Path
) -> None:
    factory, ids, context, task_id = workflow_db
    root, frozen = honest_repo
    _freeze_binding(factory, context, frozen)
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session, generation_id=context["generation_id"], run_id=run_id,
            owner="gpt-action", agent="gpt",
        )
        ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="stage context test authority",
            created_at=NOW,
            external_effects_enabled=True,
        )
    service = runtime_service(factory, tmp_path)
    service.config = replace(
        service.config, honest_root=root, action_token="postgres-action-token"
    )
    body = {
        "client": {"run_id": str(run_id), "request_id": str(_next(ids))},
        "arguments": {"dish_id": str(task_id), "kind": "initial", "agent": "gpt"},
    }
    with DishHTTPServer(("127.0.0.1", 0), service, surface_mode="action") as server:
        thread = start_server_thread(server, name="stage-context-action-http")
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/action/start"
        try:
            status, first = _post_json(url, token="postgres-action-token", body=body)
            replay_status, replay = _post_json(url, token="postgres-action-token", body=body)
        finally:
            stop_server(server, thread)

    assert status == replay_status == 200, first
    assert first["ok"] is True
    context_block = first["data"]["stage_context"]
    assert context_block["available"] is True
    assert context_block["role"] == "research"
    assert context_block["protocol_release"]["honest_commit"] == frozen
    assert context_block["protocol_release"]["binding_id"] == str(context["binding_id"])
    protocol, instructions = context_block["files"]
    assert protocol["kind"] == "stage_protocol"
    assert protocol["path"] == "dish-research-protocol.md"
    assert protocol["text"] == FROZEN["dish-research-protocol.md"]
    assert "CURRENT MAIN" not in protocol["text"]
    assert protocol["sha256"] == _sha(FROZEN["dish-research-protocol.md"])
    assert protocol["sha256_verified_against_binding"] is True
    assert protocol["git_commit"] == frozen
    assert instructions["path"] == "CLAUDE.md"
    assert instructions["text"] == FROZEN["CLAUDE.md"]
    assert context_block["omitted"] == []
    assert any(
        STAGE_CONTEXT_GUIDANCE in line
        for line in first["data"]["agent_guidance"]["instructions"]
    )
    assert replay["data"]["request_replayed"] is True
    assert replay["data"]["stage_context"] == context_block


def test_verification_start_uses_cycle_binding_protocol(
    workflow_db, honest_repo, tmp_path: Path
) -> None:
    factory, ids, context, task_id = workflow_db
    root, frozen = honest_repo
    _freeze_binding(factory, context, frozen)
    author_run, verifier_run = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session, generation_id=context["generation_id"], run_id=verifier_run,
            owner="verifier-owner", agent="codex",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        _prepare_for_verification(
            port, ids, task_id=task_id,
            operation_id=started.data["operation_id"], run_id=author_run,
        )
    service = runtime_service(factory, tmp_path)
    service.config = replace(service.config, honest_root=root)
    result = service.execute_agent(
        "start",
        {
            "task_id": str(task_id),
            "kind": "verification",
            "agent": "codex",
            "independence_attestation": "I independently inspected this exact candidate.",
        },
        principal=ServicePrincipal.from_values("verifier-owner", str(verifier_run)),
        request_id=str(_next(ids)),
    )
    assert result["ok"] is True, result
    block = result["data"]["stage_context"]
    assert block["available"] is True
    assert block["role"] == "verification"
    assert block["files"][0]["path"] == "dish-verification-protocol.md"
    assert block["files"][0]["text"] == FROZEN["dish-verification-protocol.md"]


def test_unfrozen_binding_degrades_without_failing_start(workflow_db, tmp_path: Path) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
    result = runtime_service(factory, tmp_path).execute_agent(
        "start",
        {"task_id": str(task_id), "kind": "initial", "agent": "claude"},
        principal=ServicePrincipal.from_values("owner-1", str(run_id)),
        request_id=str(_next(ids)),
    )
    assert result["ok"] is True
    block = result["data"]["stage_context"]
    assert block["available"] is False
    assert "exact Honest commit" in block["reason"]
    guided = attach_connected_agent_guidance(
        {"ok": True, "command": "start", "allowed_actions": [], "data": dict(result["data"])}
    )
    lines = guided["data"]["agent_guidance"]["instructions"]
    assert not any(STAGE_CONTEXT_GUIDANCE in line for line in lines)
    assert any("dish_honest_read" in line for line in lines)


def test_sha_mismatch_against_binding_fails_closed(honest_repo) -> None:
    root, frozen = honest_repo
    tampered = _source_ids(frozen)
    tampered["protocol_files"]["research"]["sha256"] = "0" * 64
    with pytest.raises(StageContextUnavailable, match="sha256"):
        build_stage_context(
            _binding(frozen, protocol_files=tampered["protocol_files"]),
            role="research", honest_root=root,
        )


def test_unreachable_commit_and_unsafe_path_fail_closed(honest_repo) -> None:
    root, frozen = honest_repo
    with pytest.raises(StageContextUnavailable, match="unreachable"):
        build_stage_context(_binding("f" * 40), role="research", honest_root=root)
    unsafe = _source_ids(frozen)["protocol_files"]
    unsafe["research"]["path"] = "../escape.md"
    with pytest.raises(StageContextUnavailable, match="unsafe"):
        build_stage_context(
            _binding(frozen, protocol_files=unsafe), role="research", honest_root=root
        )


def test_budget_omits_claude_md_before_protocol(honest_repo) -> None:
    root, frozen = honest_repo
    protocol_bytes = len(FROZEN["dish-research-protocol.md"].encode())
    block = build_stage_context(
        _binding(frozen), role="research", honest_root=root,
        max_text_bytes=protocol_bytes,
    )
    assert [item["path"] for item in block["files"]] == ["dish-research-protocol.md"]
    assert block["omitted"][0]["path"] == "CLAUDE.md"
    with pytest.raises(StageContextUnavailable, match="budget"):
        build_stage_context(
            _binding(frozen), role="research", honest_root=root,
            max_text_bytes=protocol_bytes - 1,
        )


def test_default_budget_covers_current_protocol_sizes() -> None:
    # Current Honest stage protocols are ~13-25 KB and CLAUDE.md ~26 KB.
    assert stage_context_module.STAGE_CONTEXT_MAX_TEXT_BYTES >= 100 * 1024
