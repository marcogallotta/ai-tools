"""Shared fixtures for the asana CLI test suite.

The CLI lives at ../asana (no .py extension, executable script), so it is
loaded via importlib rather than a normal import. Each test gets a fresh
module instance (loaded, not cached in sys.modules) so the script's module-
level globals (_CLIENT, _PAT) never leak state between tests.
"""
import importlib.util
import os
import pathlib
import sqlite3
import sys
from importlib.machinery import SourceFileLoader

import pytest

CLI_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "tools" / "asana"


def pytest_addoption(parser):
    parser.addoption(
        "--fast",
        action="store_true",
        default=False,
        help="skip expensive external process, launcher, and git boundary tests",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--fast"):
        return

    selected = []
    deselected = []
    for item in items:
        if item.get_closest_marker("boundary") is None:
            selected.append(item)
        else:
            deselected.append(item)

    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


def _load_cli_module():
    loader = SourceFileLoader("asana_cli_under_test", str(CLI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def cli(monkeypatch):
    """A freshly loaded asana CLI module with ASANA_PAT set.

    The production script re-execs into its pinned virtualenv when imported
    outside that environment. Tests intentionally load the module in-process,
    so suppress only that re-exec boundary; the installed SDK contract remains
    real and is covered separately.
    """
    monkeypatch.setenv("ASANA_PAT", "test-pat-token")
    monkeypatch.delenv("ASANA_ENV", raising=False)
    monkeypatch.setattr(os, "execv", lambda *_args, **_kwargs: None)
    module = _load_cli_module()

    class PermissiveCookingGuard:
        def before_task_mutation(self, *args, **kwargs):
            return None

        def before_create_task(self, *args, **kwargs):
            return None

        def before_create_subtask(self, *args, **kwargs):
            return None

        def before_move(self, *args, **kwargs):
            return None

        def before_raw(self, *args, **kwargs):
            return None

    module._COOKING_GUARD = PermissiveCookingGuard()
    try:
        yield module
    finally:
        module.close_client()


@pytest.fixture(scope="session")
def current_database_template(tmp_path_factory):
    """Build the current empty schema once for tests that do not test migration."""
    from dish_tool import database_schema

    path = tmp_path_factory.mktemp("dish-db-template") / "current.sqlite"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = OFF")
        database_schema.migrate_database(conn)
        database_schema._validate_current_database(conn)
    finally:
        conn.close()
    return path


@pytest.fixture(autouse=True)
def close_sqlite_connections(monkeypatch, request, current_database_template):
    """Make every test own and deterministically close its SQLite handles."""
    from dish_tool import database_schema

    real_connect = sqlite3.connect
    real_migrate = database_schema.migrate_database
    opened = []

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        # Unit and integration tests exercise logical transaction, recovery,
        # locking, and schema behavior; they do not simulate power loss. Avoid
        # filesystem sync latency while preserving SQLite's transactional rules.
        conn.execute("PRAGMA synchronous = OFF")
        opened.append(conn)
        return conn

    def migrate_or_clone_current_schema(conn):
        existing = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        claimed_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tests_bootstrap = request.node.get_closest_marker("real_database_bootstrap") is not None
        if existing or claimed_version or tests_bootstrap:
            return real_migrate(conn)
        template = real_connect(current_database_template, isolation_level=None)
        try:
            template.backup(conn)
        finally:
            template.close()

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(database_schema, "migrate_database", migrate_or_clone_current_schema)
    yield
    for conn in reversed(opened):
        try:
            conn.close()
        except sqlite3.Error:
            pass


def run_cli(module, argv, monkeypatch):
    """Invoke module.main() as if invoked from the shell with the given args."""
    monkeypatch.setattr(sys, "argv", ["asana"] + argv)
    module.main()


@pytest.fixture
def run(monkeypatch):
    def _run(module, argv):
        run_cli(module, argv, monkeypatch)
    return _run

@pytest.fixture(autouse=True)
def fast_http_server_poll(monkeypatch):
    """Keep HTTP integration semantics without paying the production idle poll."""
    from dish_service.http import DishHTTPServer

    real_serve_forever = DishHTTPServer.serve_forever

    def serve_forever(self, poll_interval=0.005):
        return real_serve_forever(self, poll_interval=poll_interval)

    monkeypatch.setattr(DishHTTPServer, "serve_forever", serve_forever)
