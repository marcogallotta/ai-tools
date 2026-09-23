"""Disposable native-PostgreSQL MCP process support for serial tests."""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from alembic import command
from alembic.config import Config
from fastmcp import FastMCP
from fastmcp.server.auth import TokenVerifier
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from dish_pg.database import session_scope
from dish_pg.schema_identity import ALEMBIC_HEAD
from dish_pg.transition import ProjectionService
from dish_service.mcp_server import DishTool, SERVER_INSTRUCTIONS, SERVER_NAME, SERVER_VERSION, TOOLS
from dish_service.native_mcp_server import native_adapter_from_environment
from tests.support.postgresql.core import NOW, _bootstrap_registry, _uuid_stream

ROOT = Path(__file__).resolve().parents[3]
TEST_RELEASE = "dish-42619b9"
CURSOR_SECRET = "disposable-mcp-cursor-secret-32-bytes"


class DisposableMCPError(RuntimeError):
    """The disposable MCP boundary could not be created or cleaned up safely."""


class _StaticTestTokenVerifier(TokenVerifier):
    def __init__(self, *, token: str, owner_id: str, resource_url: str) -> None:
        super().__init__(base_url=resource_url.removesuffix("/mcp"), required_scopes=[])
        self._token = token
        self._owner_id = owner_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="dish-disposable-test-harness",
            scopes=[],
            subject=self._owner_id,
            claims={"iss": "dish-disposable-test-harness"},
        )


def _require_test_identity(*, profile: str, database_name: str) -> None:
    if profile != "test":
        raise DisposableMCPError("disposable MCP process refuses non-TEST profile")
    if (
        not database_name.startswith("dish_")
        or not database_name.endswith("_test")
        or "prod" in database_name.lower()
        or "production" in database_name.lower()
    ):
        raise DisposableMCPError(
            "disposable MCP process refuses non-disposable database identity"
        )


def _server_main(args: argparse.Namespace) -> int:
    _require_test_identity(profile=args.profile, database_name=args.database_name)
    adapter = native_adapter_from_environment(authenticated_owner_id=args.owner_id)
    server = FastMCP(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        auth=_StaticTestTokenVerifier(
            token=args.token,
            owner_id=args.owner_id,
            resource_url=f"http://127.0.0.1:{args.port}/mcp",
        ),
        tools=[DishTool(tool, adapter) for tool in TOOLS],
    )
    try:
        uvicorn.run(
            server.http_app(path="/mcp", json_response=True, stateless_http=True),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            log_level="warning",
        )
    finally:
        adapter.close()
    return 0


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _database_url(base_dsn: str, database_name: str) -> str:
    return make_url(base_dsn).set(database=database_name).render_as_string(
        hide_password=False
    )


def _admin_url(base_dsn: str) -> str:
    return make_url(base_dsn).set(database="postgres").render_as_string(
        hide_password=False
    )


def _create_database(base_dsn: str, database_name: str) -> str:
    _require_test_identity(profile="test", database_name=database_name)
    engine = create_engine(_admin_url(base_dsn), isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    finally:
        engine.dispose()
    dsn = _database_url(base_dsn, database_name)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", dsn)
    command.upgrade(config, "head")
    return dsn


def _drop_database(base_dsn: str, database_name: str) -> None:
    _require_test_identity(profile="test", database_name=database_name)
    engine = create_engine(_admin_url(base_dsn), isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
    finally:
        engine.dispose()


def _bootstrap_authority(dsn: str) -> uuid.UUID:
    engine = create_engine(dsn, future=True)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    ids = _uuid_stream()
    try:
        with session_scope(factory) as session:
            context = _bootstrap_registry(
                session,
                ids,
                generation_status="active",
                schema_head=ALEMBIC_HEAD,
            )
            ProjectionService(session, uuid_factory=lambda: next(ids)).activate_epoch(
                generation_id=context["generation_id"],
                activation_reason="disposable MCP process harness",
                created_at=NOW,
                external_effects_enabled=True,
            )
            return context["generation_id"]
    finally:
        engine.dispose()


@dataclass(frozen=True, slots=True)
class TeardownReceipt:
    pid: int
    process_group_id: int
    exit_code: int
    forced_kill: bool
    descendants_remaining: tuple[int, ...]
    database_name: str
    database_dropped: bool


class DisposableMCPProcess:
    """Own one unique TEST database, MCP process group, and teardown receipt."""

    def __init__(self, *, base_dsn: str, root: Path) -> None:
        suffix = uuid.uuid4().hex[:16]
        self.database_name = f"dish_mcp_{suffix}_test"
        self.base_dsn = base_dsn
        self.root = root
        self.port = _free_loopback_port()
        self.token = secrets.token_urlsafe(32)
        self.owner_id = str(secrets.randbelow(900_000) + 100_000)
        self.process: subprocess.Popen[str] | None = None
        self.dsn: str | None = None
        self.generation_id: uuid.UUID | None = None
        self.receipt: TeardownReceipt | None = None
        self.log_path = root / "mcp-process.log"

    def start(self) -> "DisposableMCPProcess":
        self.root.mkdir(parents=True, exist_ok=True)
        state_dir = self.root / "state"
        state_dir.mkdir()
        try:
            self.dsn = _create_database(self.base_dsn, self.database_name)
            self.generation_id = _bootstrap_authority(self.dsn)
            env = {
                key: value
                for key, value in os.environ.items()
                if "ASANA" not in key.upper()
            }
            env.update(
                {
                    "DISH_PROFILE": "test",
                    "DISH_PG_DATABASE_URL": self.dsn,
                    "DISH_PG_EXPECTED_DATABASE_NAME": self.database_name,
                    "DISH_PG_EXPECTED_SCHEMA_HEAD": ALEMBIC_HEAD,
                    "DISH_PG_EXPECTED_RELEASE": TEST_RELEASE,
                    "DISH_PG_EXPECTED_GENERATION_ID": str(self.generation_id),
                    "DISH_PG_CURSOR_SECRET": CURSOR_SECRET,
                    "DISH_PG_AUTHORITY_STATE_DIR": str(state_dir),
                }
            )
            command_line = [
                sys.executable,
                "-m",
                "tests.support.postgresql.mcp_process",
                "serve",
                "--profile",
                "test",
                "--database-name",
                self.database_name,
                "--port",
                str(self.port),
                "--token",
                self.token,
                "--owner-id",
                self.owner_id,
            ]
            log = self.log_path.open("w", encoding="utf-8")
            try:
                self.process = subprocess.Popen(
                    command_line,
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            finally:
                log.close()
            self._wait_for_tcp()
        except BaseException:
            if self.process is not None and self.process.poll() is None:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                self.process.wait(timeout=5)
            if self.dsn is not None:
                _drop_database(self.base_dsn, self.database_name)
            raise
        return self

    def _wait_for_tcp(self, timeout: float = 15.0) -> None:
        assert self.process is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            return_code = self.process.poll()
            if return_code is not None:
                raise DisposableMCPError(
                    f"MCP process exited before readiness ({return_code}): "
                    f"{self.log_path.read_text(encoding='utf-8')}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise DisposableMCPError("MCP process did not become ready before timeout")

    async def _create_readback(self) -> tuple[dict[str, Any], dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx2.AsyncClient(headers=headers) as client:
            async with streamable_http_client(
                f"http://127.0.0.1:{self.port}/mcp", http_client=client
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    run_id = str(uuid.uuid4())
                    created_result = await session.call_tool(
                        "dish_create",
                        {
                            "client": {
                                "run_id": run_id,
                                "request_id": str(uuid.uuid4()),
                            },
                            "arguments": {
                                "agent": "codex",
                                "title": "Disposable MCP process proof",
                            },
                        },
                    )
                    created = created_result.structured_content
                    if created_result.is_error or not isinstance(created, dict):
                        raise DisposableMCPError(f"dish_create failed: {created_result}")
                    dish_id = created.get("data", {}).get("dish_id")
                    if not isinstance(dish_id, str):
                        raise DisposableMCPError("dish_create omitted canonical dish_id")
                    read_result = await session.call_tool(
                        "dish_read",
                        {
                            "client": {"run_id": run_id},
                            "arguments": {"agent": "codex", "dish_id": dish_id},
                        },
                    )
                    readback = read_result.structured_content
                    if read_result.is_error or not isinstance(readback, dict):
                        raise DisposableMCPError(f"dish_read failed: {read_result}")
                    return created, readback

    def create_readback(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return asyncio.run(self._create_readback())

    def stop(self) -> TeardownReceipt:
        if self.receipt is not None:
            return self.receipt
        if self.process is None:
            raise DisposableMCPError("MCP process was never started")
        process = self.process
        pgid = os.getpgid(process.pid)
        forced = False
        if process.poll() is None:
            os.killpg(pgid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                forced = True
                os.killpg(pgid, signal.SIGKILL)
                process.wait(timeout=5)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            descendants: tuple[int, ...] = ()
        else:
            descendants = (pgid,)
        _drop_database(self.base_dsn, self.database_name)
        self.receipt = TeardownReceipt(
            pid=process.pid,
            process_group_id=pgid,
            exit_code=int(process.returncode),
            forced_kill=forced,
            descendants_remaining=descendants,
            database_name=self.database_name,
            database_dropped=True,
        )
        return self.receipt

    def __enter__(self) -> "DisposableMCPProcess":
        return self.start()

    def __exit__(self, _type, _value, _traceback) -> None:
        self.stop()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--profile", required=True)
    serve.add_argument("--database-name", required=True)
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--token", required=True)
    serve.add_argument("--owner-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return _server_main(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DisposableMCPError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
