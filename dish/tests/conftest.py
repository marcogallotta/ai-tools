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

from tests.flake_policy import (
    CANDIDATE_MARKER,
    QUARANTINE_MARKER,
    validate_marker_metadata,
)
from importlib.machinery import SourceFileLoader

import pytest

CLI_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "tools" / "asana"

REQUIRED_DATABASE_BOUNDARY_CATEGORIES = {
    "database_boundary_bootstrap",
    "database_boundary_upgrade",
    "database_boundary_concurrency",
    "database_boundary_durability",
}

REQUIRED_SMOKE_INVARIANTS = {
    "invariant_request_replay",
    "invariant_lease_authority",
    "invariant_transaction_rollback",
    "invariant_partial_effect_recovery",
    "invariant_submission_terminal_proof",
    "invariant_authorization",
    "invariant_planning_intent",
    "invariant_abandonment",
    "invariant_database_bootstrap",
    "invariant_backup_restore",
}


def pytest_addoption(parser):
    parser.addoption(
        "--smoke",
        action="store_true",
        default=False,
        help="run only tests explicitly marked as smoke",
    )
    parser.addoption(
        "--database-boundary",
        action="store_true",
        default=False,
        help="run the real SQLite bootstrap, migration, concurrency, and durability lane",
    )
    parser.addoption(
        "--flake-candidates",
        action="store_true",
        default=False,
        help="run only tests under active flaky-test investigation",
    )
    parser.addoption(
        "--quarantine",
        action="store_true",
        default=False,
        help="run only confirmed, time-bounded quarantined flaky tests",
    )


def _flake_policy_violations(items):
    violations = []
    for item in items:
        candidate = item.get_closest_marker(CANDIDATE_MARKER)
        quarantine = item.get_closest_marker(QUARANTINE_MARKER)
        if candidate is not None and quarantine is not None:
            violations.append(
                f"{item.nodeid}: cannot be both flake_candidate and quarantined"
            )
            continue
        marker = quarantine or candidate
        if marker is None:
            continue
        launch_critical = (
            item.get_closest_marker("smoke") is not None
            or any(mark.name.startswith("invariant_") for mark in item.iter_markers())
        )
        for error in validate_marker_metadata(
            marker.name, marker.kwargs, launch_critical=launch_critical
        ):
            violations.append(f"{item.nodeid}: {error}")
    return violations


def _select_items(config, items):
    smoke_requested = config.getoption("--smoke")
    database_boundary_requested = config.getoption("--database-boundary")
    candidates_requested = config.getoption("--flake-candidates")
    quarantine_requested = config.getoption("--quarantine")
    selectors = [
        smoke_requested,
        database_boundary_requested,
        candidates_requested,
        quarantine_requested,
    ]
    if sum(bool(value) for value in selectors) > 1:
        raise pytest.UsageError(
            "--smoke, --database-boundary, --flake-candidates, and --quarantine "
            "are separate test lanes"
        )

    if smoke_requested:
        selected = [
            item
            for item in items
            if item.get_closest_marker("smoke") is not None
            and item.get_closest_marker("full_suite_only") is None
            and item.get_closest_marker(QUARANTINE_MARKER) is None
        ]
        covered = {
            marker
            for item in selected
            for marker in REQUIRED_SMOKE_INVARIANTS
            if item.get_closest_marker(marker) is not None
        }
        missing = REQUIRED_SMOKE_INVARIANTS - covered
        if missing:
            raise pytest.UsageError(
                "smoke suite lacks required invariant coverage: "
                + ", ".join(sorted(missing))
            )
        return selected

    if database_boundary_requested:
        selected = [
            item
            for item in items
            if item.get_closest_marker("database_boundary") is not None
            and item.get_closest_marker(QUARANTINE_MARKER) is None
        ]
        covered = {
            marker
            for item in selected
            for marker in REQUIRED_DATABASE_BOUNDARY_CATEGORIES
            if item.get_closest_marker(marker) is not None
        }
        missing = REQUIRED_DATABASE_BOUNDARY_CATEGORIES - covered
        if missing:
            raise pytest.UsageError(
                "database-boundary lane lacks required coverage: "
                + ", ".join(sorted(missing))
            )
        return selected

    if candidates_requested:
        return [
            item
            for item in items
            if item.get_closest_marker(CANDIDATE_MARKER) is not None
            and item.get_closest_marker(QUARANTINE_MARKER) is None
        ]

    if quarantine_requested:
        return [
            item
            for item in items
            if item.get_closest_marker(QUARANTINE_MARKER) is not None
        ]

    return [
        item
        for item in items
        if item.get_closest_marker(QUARANTINE_MARKER) is None
    ]


def pytest_collection_modifyitems(config, items):
    violations = _flake_policy_violations(items)
    if violations:
        raise pytest.UsageError(
            "invalid flaky-test policy metadata:\n" + "\n".join(violations)
        )

    selected = _select_items(config, items)
    selected_set = set(selected)
    deselected = [item for item in items if item not in selected_set]
    items[:] = selected
    if deselected:
        config.hook.pytest_deselected(items=deselected)


def pytest_sessionfinish(session, exitstatus):
    if exitstatus != pytest.ExitCode.NO_TESTS_COLLECTED:
        return
    config = session.config
    if config.getoption("--flake-candidates") or config.getoption("--quarantine"):
        session.exitstatus = pytest.ExitCode.OK



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

    uses_production_pragmas = (
        request.node.get_closest_marker("production_sqlite_pragmas") is not None
    )

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        try:
            if not uses_production_pragmas:
                # Fast logical lane: preserve SQLite transaction and locking rules
                # without paying filesystem synchronization latency. Tests marked
                # ``production_sqlite_pragmas`` retain the production setting.
                conn.execute("PRAGMA synchronous = OFF")
        except Exception:
            opened.remove(conn)
            conn.close()
            raise
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
