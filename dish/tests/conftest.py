"""Shared fixtures for the asana CLI test suite.

The CLI lives at ../asana (no .py extension, executable script), so it is
loaded via importlib rather than a normal import. Each test gets a fresh
module instance (loaded, not cached in sys.modules) so the script's module-
level globals (_CLIENT, _PAT) never leak state between tests.
"""
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys

from tests.flake_policy import (
    CANDIDATE_MARKER,
    QUARANTINE_MARKER,
    validate_marker_metadata,
)
from tests.support.postgresql.certification import (
    NATIVE_POSTGRESQL_UNAVAILABLE,
    NativePostgreSQLUnavailable,
    postgresql_dsn,
    probe_native_postgresql,
    redacted_dsn,
)
from importlib.machinery import SourceFileLoader

import pytest

from test_selection.execution_guard import TestExecutionRefused, require_safe_test_checkout

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
    "invariant_workflow_action_authority",
}


def pytest_sessionstart(session):
    try:
        require_safe_test_checkout(pathlib.Path(__file__).resolve().parents[1])
    except TestExecutionRefused as exc:
        raise pytest.UsageError(f"REFUSED: {exc}") from exc


def pytest_addoption(parser):
    parser.addoption(
        "--smoke",
        action="store_true",
        default=False,
        help="run only the launch-critical invariant smoke core",
    )
    parser.addoption(
        "--database-boundary",
        action="store_true",
        default=False,
        help="run the real SQLite bootstrap, migration, concurrency, and durability lane",
    )
    parser.addoption(
        "--postgresql",
        action="store_true",
        default=False,
        help=(
            "run postgresql-marked fixtures against a real PostgreSQL instance "
            "(see deploy/postgresql/compose.yaml) instead of the SQLite-rendered lane; "
            "DSN overridable via DISH_TEST_POSTGRESQL_DSN"
        ),
    )
    parser.addoption(
        "--native-postgresql",
        action="store_true",
        default=False,
        help="run only the governed native PostgreSQL certification inventory",
    )
    parser.addoption(
        "--postgresql-report",
        type=pathlib.Path,
        default=None,
        help="write exact native PostgreSQL execution accounting as JSON",
    )
    parser.addoption(
        "--pglite",
        action="store_true",
        default=False,
        help="run the non-certification PGlite PostgreSQL-semantic development lane",
    )
    parser.addoption(
        "--dish-internal-governed-node",
        default=None,
        help="internal governed-runner exact node selection",
    )
    parser.addoption(
        "--dish-internal-native-test-file",
        action="append",
        default=[],
        help="internal governed-runner native PostgreSQL test-file selection",
    )
    parser.addoption(
        "--dish-internal-inventory-report",
        type=pathlib.Path,
        default=None,
        help="internal governed-runner inventory output",
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
    pglite_requested = config.getoption("--pglite")
    native_postgresql_requested = config.getoption("--native-postgresql")
    selectors = [
        smoke_requested,
        database_boundary_requested,
        candidates_requested,
        quarantine_requested,
        pglite_requested,
        native_postgresql_requested,
    ]
    if sum(bool(value) for value in selectors) > 1:
        raise pytest.UsageError(
            "--smoke, --database-boundary, --flake-candidates, --quarantine, "
            "--pglite, and --native-postgresql are separate test lanes"
        )

    if native_postgresql_requested:
        if not config.getoption("--postgresql"):
            raise pytest.UsageError(
                "--native-postgresql requires --postgresql; SQLite and PGlite cannot certify the lane"
            )
        return [
            item
            for item in items
            if item.get_closest_marker("native_postgresql") is not None
            and item.get_closest_marker(QUARANTINE_MARKER) is None
        ]

    if smoke_requested:
        selected = [
            item
            for item in items
            if any(
                item.get_closest_marker(marker) is not None
                for marker in REQUIRED_SMOKE_INVARIANTS
            )
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

    if pglite_requested:
        return [
            item
            for item in items
            if item.get_closest_marker("pglite") is not None
            and item.get_closest_marker(QUARANTINE_MARKER) is None
        ]

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
        and item.get_closest_marker("pglite") is None
    ]


def _is_complete_repository_collection(config) -> bool:
    root = pathlib.Path(str(config.rootpath)).resolve()
    roots = {root, root / "tests"}
    return len(config.args) == 1 and pathlib.Path(config.args[0]).resolve() in roots


def _internal_governed_runner_requested(config) -> bool:
    return bool(
        config.getoption("--dish-internal-governed-node")
        or config.getoption("--dish-internal-native-test-file")
        or config.getoption("--dish-internal-inventory-report")
    )


def _validate_internal_governed_runner(config) -> None:
    if not _internal_governed_runner_requested(config):
        return
    if os.environ.get("DISH_INTERNAL_GOVERNED_RUNNER") != "1":
        raise pytest.UsageError(
            "internal governed-runner options may be used only by repository lane scripts"
        )
    native_files = config.getoption("--dish-internal-native-test-file")
    if native_files:
        if not (config.getoption("--native-postgresql") and config.getoption("--postgresql")):
            raise pytest.UsageError(
                "internal native test-file selection requires --postgresql --native-postgresql"
            )
    if (
        config.getoption("--dish-internal-governed-node")
        or config.getoption("--dish-internal-inventory-report")
    ) and not (config.getoption("--pglite") or config.getoption("--quarantine")):
        raise pytest.UsageError(
            "internal governed node/inventory options require --pglite or --quarantine"
        )


def pytest_collection_modifyitems(config, items):
    _validate_internal_governed_runner(config)
    violations = _flake_policy_violations(items)
    if not _is_complete_repository_collection(config) and any(
        config.getoption(name)
        for name in (
            "--smoke",
            "--database-boundary",
            "--pglite",
            "--native-postgresql",
        )
    ):
        raise pytest.UsageError(
            "governed lanes require complete repository collection; do not combine lane selectors "
            "with explicit test paths"
        )
    if violations:
        raise pytest.UsageError(
            "invalid flaky-test policy metadata:\n" + "\n".join(violations)
        )

    selected = _select_items(config, items)
    governed_inventory = sorted(item.nodeid for item in selected)
    config._governed_runner_inventory = governed_inventory
    native_files = set(config.getoption("--dish-internal-native-test-file"))
    if native_files:
        available_files = {item.nodeid.split("::", 1)[0] for item in selected}
        missing_files = sorted(native_files - available_files)
        if missing_files:
            raise pytest.UsageError(
                "internal native test files are not in the governed inventory: "
                + ", ".join(missing_files)
            )
        selected = [
            item for item in selected if item.nodeid.split("::", 1)[0] in native_files
        ]
    internal_node = config.getoption("--dish-internal-governed-node")
    if internal_node is not None:
        exact = [item for item in selected if item.nodeid == internal_node]
        if len(exact) != 1:
            raise pytest.UsageError(
                f"internal governed node {internal_node!r} is not in the selected lane inventory"
            )
        selected = exact

    native_nodeids = {
        item.nodeid
        for item in selected
        if item.get_closest_marker("native_postgresql") is not None
    }
    config._native_postgresql_state = {
        "selected_nodeids": sorted(native_nodeids),
        "outcomes": {},
        "skip_reasons": {},
    }
    selected_set = set(selected)
    deselected = [item for item in items if item not in selected_set]
    items[:] = selected
    if deselected:
        config.hook.pytest_deselected(items=deselected)


def pytest_collection_finish(session):
    inventory_output = session.config.getoption("--dish-internal-inventory-report")
    if (
        inventory_output is not None
        and os.environ.get("DISH_INTERNAL_GOVERNED_RUNNER") == "1"
    ):
        payload = {
            "format": "dish-governed-pytest-inventory-v1",
            "selector": (
                "pglite" if session.config.getoption("--pglite") else "quarantine"
            ),
            "nodeids": list(
                getattr(session.config, "_governed_runner_inventory", ())
            ),
            "selected_nodeids": [item.nodeid for item in session.items],
        }
        inventory_output.parent.mkdir(parents=True, exist_ok=True)
        inventory_output.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    state = getattr(session.config, "_native_postgresql_state", None)
    if state is None:
        return
    state["selected_nodeids"] = sorted(
        item.nodeid
        for item in session.items
        if item.get_closest_marker("native_postgresql") is not None
    )


def _native_postgresql_summary(config):
    state = getattr(
        config,
        "_native_postgresql_state",
        {"selected_nodeids": [], "outcomes": {}, "skip_reasons": {}},
    )
    selected = list(state["selected_nodeids"])
    outcomes = dict(state["outcomes"])
    skip_reasons = dict(state["skip_reasons"])
    executed = sorted(nodeid for nodeid, outcome in outcomes.items() if outcome in {"passed", "failed"})
    skipped = sorted(nodeid for nodeid, outcome in outcomes.items() if outcome == "skipped")
    unavailable = sorted(
        nodeid
        for nodeid in skipped
        if NATIVE_POSTGRESQL_UNAVAILABLE in skip_reasons.get(nodeid, "")
    )
    failed = sorted(nodeid for nodeid, outcome in outcomes.items() if outcome == "failed")
    errors = sorted(nodeid for nodeid, outcome in outcomes.items() if outcome == "error")
    passed = sorted(nodeid for nodeid, outcome in outcomes.items() if outcome == "passed")
    return {
        "selected": len(selected),
        "executed": len(executed),
        "passed": len(passed),
        "failed": len(failed),
        "errors": len(errors),
        "skipped": len(skipped),
        "unavailable": len(unavailable),
        "selected_nodeids": selected,
        "executed_nodeids": executed,
        "passed_nodeids": passed,
        "failed_nodeids": failed,
        "error_nodeids": errors,
        "skipped_nodeids": skipped,
        "unavailable_nodeids": unavailable,
        "skip_reasons": skip_reasons,
    }


_ACTIVE_PYTEST_CONFIG = None


def pytest_configure(config):
    global _ACTIVE_PYTEST_CONFIG
    _ACTIVE_PYTEST_CONFIG = config


def pytest_unconfigure(config):
    global _ACTIVE_PYTEST_CONFIG
    if _ACTIVE_PYTEST_CONFIG is config:
        _ACTIVE_PYTEST_CONFIG = None


def pytest_runtest_logreport(report):
    config = _ACTIVE_PYTEST_CONFIG
    if config is None:
        return
    state = getattr(config, "_native_postgresql_state", None)
    if state is None or report.nodeid not in state["selected_nodeids"]:
        return
    if report.when == "call":
        state["outcomes"][report.nodeid] = "passed" if report.passed else "failed"
    elif report.when == "setup" and report.skipped:
        state["outcomes"][report.nodeid] = "skipped"
        state["skip_reasons"][report.nodeid] = str(report.longrepr)
    elif report.when == "setup" and report.failed:
        state["outcomes"][report.nodeid] = "error"


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and (
        config.getoption("--flake-candidates")
        or config.getoption("--quarantine")
        or config.getoption("--pglite")
    ):
        session.exitstatus = pytest.ExitCode.OK

    output = config.getoption("--postgresql-report")
    if output is not None:
        identity = getattr(config, "_native_postgresql_identity", None)
        payload = {
            "format": "dish-native-postgresql-pytest-report-v1",
            "requested": bool(config.getoption("--native-postgresql")),
            "postgresql_enabled": bool(config.getoption("--postgresql")),
            "dsn": redacted_dsn(postgresql_dsn()),
            "identity": identity,
            "tests": _native_postgresql_summary(config),
            "pytest_exit_status": int(session.exitstatus),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def pytest_terminal_summary(terminalreporter):
    summary = _native_postgresql_summary(terminalreporter.config)
    if summary["selected"]:
        terminalreporter.write_sep(
            "=",
            "native PostgreSQL: "
            f"selected={summary['selected']} executed={summary['executed']} "
            f"passed={summary['passed']} failed={summary['failed']} errors={summary['errors']} "
            f"skipped={summary['skipped']} unavailable={summary['unavailable']}",
        )



@pytest.fixture(scope="session")
def native_postgresql_identity(request):
    if not request.config.getoption("--postgresql"):
        pytest.skip(NATIVE_POSTGRESQL_UNAVAILABLE)
    try:
        identity = probe_native_postgresql()
    except NativePostgreSQLUnavailable as exc:
        pytest.fail(f"native PostgreSQL identity check failed: {exc}")
    request.config._native_postgresql_identity = identity.as_dict()
    return identity


@pytest.fixture(autouse=True)
def require_native_postgresql(request):
    if request.node.get_closest_marker("native_postgresql") is None:
        return None
    if not request.config.getoption("--postgresql"):
        pytest.skip(NATIVE_POSTGRESQL_UNAVAILABLE)
    return request.getfixturevalue("native_postgresql_identity")


@pytest.fixture
def sqlite_migration_database(tmp_path):
    from tests.support.postgresql.migrations import MigrationDatabase

    return MigrationDatabase(
        sqlalchemy_url=f"sqlite+pysqlite:///{tmp_path / 'migration.sqlite3'}",
        expected_dialect="sqlite",
        certification_evidence=False,
        lane="sqlite_compatibility",
    )


@pytest.fixture
def pglite_migration_database(pglite):
    from tests.support.postgresql.migrations import MigrationDatabase

    return MigrationDatabase(
        sqlalchemy_url=pglite.sqlalchemy_url,
        expected_dialect="postgresql",
        certification_evidence=False,
        lane="pglite_development",
    )


@pytest.fixture
def native_migration_database(request, native_postgresql_identity):
    from tests.support.postgresql.certification import postgresql_dsn
    from tests.support.postgresql.migrations import MigrationDatabase

    return MigrationDatabase(
        sqlalchemy_url=postgresql_dsn(),
        expected_dialect="postgresql",
        certification_evidence=True,
        lane="native_postgresql_certification",
    )


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


@pytest.fixture(autouse=True)
def isolate_dish_client_profile_env(monkeypatch):
    """Prevent ambient client-profile env vars from leaking into tests.

    Agent shells default to DISH_PROFILE=prod with real DISH_SERVICE_URL_PROD
    and DISH_*_TOKEN_PROD credentials loaded from ~/.bashrc. Without this,
    a test that only clears the legacy DISH_SERVICE_URL/DISH_SERVICE_TOKEN
    names falls through resolve_client_profile() to the real production
    service instead of whatever it monkeypatched.
    """
    for name in (
        "DISH_PROFILE",
        "DISH_SERVICE_URL_TEST",
        "DISH_SERVICE_URL_PROD",
        "DISH_SERVICE_TOKEN_TEST",
        "DISH_SERVICE_TOKEN_PROD",
        "DISH_ADMIN_TOKEN_TEST",
        "DISH_ADMIN_TOKEN_PROD",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def current_database_template(tmp_path_factory):
    """Build the current empty schema once for tests that do not test migration."""
    from dish_tool import database_migrations
    from dish_tool.database_schema_validation import validate_current_database

    path = tmp_path_factory.mktemp("dish-db-template") / "current.sqlite"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = OFF")
        database_migrations.migrate_database(conn)
        validate_current_database(conn)
    finally:
        conn.close()
    return path


@pytest.fixture(autouse=True)
def close_sqlite_connections(
    monkeypatch, request, current_database_template, require_native_postgresql
):
    """Make every test own and deterministically close its SQLite handles."""
    from dish_tool import database_migrations, database_initialization

    real_connect = sqlite3.connect
    real_migrate = database_migrations.migrate_database
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
    monkeypatch.setattr(database_migrations, "migrate_database", migrate_or_clone_current_schema)
    monkeypatch.setattr(database_initialization, "migrate_database", migrate_or_clone_current_schema)
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
