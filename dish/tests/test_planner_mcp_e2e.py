"""Actual authenticated MCP HTTP calls against an isolated PostgreSQL planner.

Uses the production app factory/registration, replacing only external OAuth token
verification. No production database, credentials, or planner records are used.
"""
from __future__ import annotations

import asyncio
import copy
import json
import secrets
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx2
import psycopg
import pytest
import uvicorn
from fastmcp.server.auth import TokenVerifier
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from psycopg.types.json import Jsonb

from dish_service import mcp_server
from tests.support.thread_teardown import join_thread, start_thread

TOKEN = "isolated-planner-mcp-token"


class _Verifier(TokenVerifier):
    def __init__(self, **kwargs):
        super().__init__(base_url=kwargs["base_url"], required_scopes=[])
        self.owner = kwargs["allowed_user_id"]

    async def verify_token(self, token):
        if not secrets.compare_digest(token, TOKEN):
            return None
        return AccessToken(
            token=token, client_id="planner-test", scopes=[], subject=self.owner,
        )


class _NoBackendCalls:
    def call(self, *_args, **_kwargs):
        raise AssertionError("Planner read must not invoke recipe workflows")


@pytest.fixture(scope="module")
def planner_database(tmp_path_factory):
    container = subprocess.check_output(
        ["docker", "run", "--rm", "-d", "-e", "POSTGRES_PASSWORD=isolated",
         "-e", "POSTGRES_DB=planner_test", "-p", "127.0.0.1::5432", "postgres:17.10"], text=True,
    ).strip()
    try:
        address = subprocess.check_output(
            ["docker", "port", container, "5432/tcp"], text=True,
        ).strip()
        admin = f"postgresql://postgres:isolated@{address}/planner_test"
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(admin, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("CREATE SCHEMA cook_planner")
            conn.execute("""CREATE TABLE cook_planner.plans (
                id text PRIMARY KEY, revision bigint NOT NULL,
                document jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())""")
            conn.execute("CREATE TABLE cook_planner.history (revision bigint NOT NULL)")
        private = tmp_path_factory.mktemp("planner-reader")
        admin_file = private / "admin-dsn"
        admin_file.write_text(admin)
        admin_file.chmod(0o600)
        output = private / "mcp.env"
        command = [
            sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/dish-planner-reader-setup"),
            "--admin-dsn-file", str(admin_file), "--expected-database", "planner_test",
            "--output-env", str(output),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        assert output.stat().st_mode & 0o777 == 0o600
        credential = output.read_bytes()
        reader = json.loads(output.read_text().split("=", 1)[1])
        again = subprocess.run(command, check=False, capture_output=True, text=True)
        assert again.returncode != 0
        assert output.read_bytes() == credential
        with psycopg.connect(reader) as conn:
            assert conn.execute("SELECT current_user").fetchone()[0] == "cook_planner_mcp_reader"
        yield admin, reader
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=True, capture_output=True)


@pytest.fixture
def mcp_endpoint(planner_database, monkeypatch, tmp_path):
    admin, reader = planner_database
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("TRUNCATE cook_planner.plans, cook_planner.history")
    monkeypatch.setenv("DISH_MCP_PLANNER_DATABASE_URL", reader)
    monkeypatch.setenv("DISH_MCP_PLANNING_CONTEXT_DB", str(tmp_path / "receipts.sqlite"))
    monkeypatch.setattr(mcp_server, "DishGitHubProvider", _Verifier)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    config = mcp_server.MCPAuthConfig(
        github_client_id="test", github_client_secret="test", github_user_id="192548",
        resource_url=url, bind_port=port,
    )
    app = mcp_server.create_app(_NoBackendCalls(), config)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = start_thread(server.run, kwargs={"sockets": [listener]}, daemon=True)
    deadline = time.monotonic() + 10
    try:
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise AssertionError("Production MCP app failed to start")
            time.sleep(0.02)
        yield url, admin
    finally:
        server.should_exit = True
        try:
            join_thread(thread, timeout=10)
        finally:
            listener.close()


@asynccontextmanager
async def _session(url):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def _state():
    return {
        "version": 1, "revision": 42,
        "startDate": "2026-11-27", "endDate": "2026-12-10",
        "batches": [{"id": "pasta", "title": "Pasta sauce", "cookDate": "2026-11-29"}],
        "portions": [
            {"id": "p1", "batchId": "pasta", "date": "2026-11-29", "track": 1},
            {"id": "p2", "batchId": "pasta", "date": "2026-12-07", "track": 1},
            {"id": "p3", "batchId": "pasta", "date": None, "track": 1, "held": True},
        ],
        "source": {"importedAt": "2026-11-27T12:00:00Z", "note": "User's saved board"},
        "travelDates": ["2026-11-30", "2026-12-01"],
        "travel": {"away": {"version": 1, "batches": [], "portions": [],
                            "annotations": [{"text": "Away board note"}]},
                   "departureDate": "2026-11-30", "returnDate": "2026-12-07",
                   "returnDateTentative": True, "destination": "UK"},
        "preparations": [{"id": "peel", "title": "Candied peel", "done": False}],
        "extras": [{"id": "bread", "title": "Panettone", "cookDate": "2026-11-29"}],
        "dependencies": [{"before": "peel", "after": "bread"}],
        "annotations": [{"text": "Do not move anything"}],
    }


def _put(admin, state, revision=7, plan_id="main"):
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO cook_planner.plans (id, revision, document) VALUES (%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET revision=EXCLUDED.revision,
            document=EXCLUDED.document,updated_at=now()""",
            (plan_id, revision, Jsonb(state)),
        )


def test_real_mcp_discovery_exact_fresh_snapshot_and_no_writes(mcp_endpoint):
    url, admin = mcp_endpoint
    original = _state()
    _put(admin, original)
    _put(admin, {"private": "not-the-main-plan"}, plan_id="other")
    with psycopg.connect(admin) as conn:
        before = conn.execute("SELECT * FROM cook_planner.plans ORDER BY id").fetchall()
        history = conn.execute("SELECT * FROM cook_planner.history").fetchall()

    async def journey():
        async with _session(url) as session:
            discovered = await session.list_tools()
            tool = next(t for t in discovered.tools if t.name == "dish_planner_read")
            assert tool.input_schema["additionalProperties"] is False
            assert tool.input_schema.get("properties", {}) == {}
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            result = await session.call_tool("dish_planner_read", {})
            snapshot = result.structured_content
            assert not result.is_error
            assert snapshot["ok"] is True
            assert snapshot["revision"] == 7
            assert snapshot["state"] == original
            assert snapshot["state"]["revision"] == 42
            assert datetime.fromisoformat(snapshot["read_at"].replace("Z", "+00:00"))
            assert snapshot["updated_at"]
            assert snapshot["source"]
            assert snapshot["semantics"]
            with psycopg.connect(admin) as conn:
                assert conn.execute("SELECT * FROM cook_planner.plans ORDER BY id").fetchall() == before
                assert conn.execute("SELECT * FROM cook_planner.history").fetchall() == history
            changed = copy.deepcopy(original)
            changed["annotations"].append({"text": "Changed after the first MCP read"})
            _put(admin, changed, revision=8)
            fresh = (await session.call_tool("dish_planner_read", {})).structured_content
            assert fresh["revision"] == 8
            assert fresh["state"] == changed
            assert "not-the-main-plan" not in json.dumps(fresh)
            bad = await session.call_tool("dish_planner_read", {"plan_id": "other"})
            assert bad.is_error or (bad.structured_content or {}).get("ok") is False
            assert "not-the-main-plan" not in str(bad)

    asyncio.run(journey())


def test_real_mcp_requires_authentication(mcp_endpoint):
    url, _ = mcp_endpoint
    for headers in ({}, {"Authorization": "Bearer wrong-token"}):
        response = httpx2.post(
            url, headers={"Accept": "application/json, text/event-stream", **headers},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "dish_planner_read", "arguments": {}}},
        )
        assert response.status_code == 401


def test_real_mcp_missing_plan_and_unsupported_record_are_explicit(mcp_endpoint):
    url, admin = mcp_endpoint

    async def journey():
        async with _session(url) as session:
            missing = (await session.call_tool("dish_planner_read", {})).structured_content
            assert missing["code"] == "PLANNER_NOT_INITIALIZED"
            for state in ({"version": 999, "batches": [], "portions": []}, {"version": 1}):
                _put(admin, state)
                bad = (await session.call_tool("dish_planner_read", {})).structured_content
                assert bad["code"] == "PLANNER_UNSUPPORTED_STATE"
                assert "state" not in bad

    asyncio.run(journey())


def test_database_reader_role_cannot_write_or_read_history(planner_database):
    _, reader = planner_database
    for sql in (
        "INSERT INTO cook_planner.plans(id,revision,document) VALUES ('bad',1,'{}')",
        "UPDATE cook_planner.plans SET revision=999",
        "DELETE FROM cook_planner.plans",
        "SELECT * FROM cook_planner.history",
        "CREATE TABLE cook_planner.bad(id int)",
    ):
        with psycopg.connect(reader, autocommit=True) as conn:
            conn.execute("SET default_transaction_read_only = off")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
