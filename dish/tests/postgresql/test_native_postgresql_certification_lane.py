from __future__ import annotations

import hashlib
import json
import runpy
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tests.support.postgresql.certification as certification
from test_selection import execution_guard
from tests.support.postgresql.certification import (
    discover_native_postgresql_inventory,
    NativePostgreSQLIdentity,
    NativePostgreSQLUnavailable,
    postgresql_dsn,
    probe_native_postgresql,
    redacted_dsn,
)
from test_selection.execution_guard import require_safe_test_checkout

pytestmark = pytest.mark.smoke
ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_HEAD = require_safe_test_checkout(ROOT)


def _cert_args(*args: str) -> list[str]:
    return ["--expected-head", CANDIDATE_HEAD, *args]

TEST_NODEID = "tests/postgresql/native/test_governed_waiver.py::test_governed_skip"
PASS_NODEID = "tests/postgresql/native/test_governed_waiver.py::test_governed_pass"
OWNER_TASK_GID = "1217428310522281"


def _probe_engine(database: str) -> MagicMock:
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    engine.dialect.driver = "psycopg"
    row = (
        engine.connect.return_value.__enter__.return_value.execute.return_value
        .mappings.return_value.one
    )
    row.return_value = {
        "database": database,
        "server_version": "17.10",
        "server_version_full": "PostgreSQL 17.10 on x86_64-pc-linux-gnu",
        "server_address": "127.0.0.1",
        "server_port": 55432,
    }
    return engine


def _waiver(nodeid: str, reason: str, *, review_by: str = "2099-01-01") -> str:
    return json.dumps(
        {
            "nodeid": nodeid,
            "expected_reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            "owner_task_gid": OWNER_TASK_GID,
            "review_by": review_by,
            "justification": "deterministic certification test waiver",
        },
        sort_keys=True,
    )


def _run_fake_certification(
    monkeypatch,
    tmp_path: Path,
    *,
    skip_reason: str | None = None,
    waivers: tuple[str, ...] = (),
) -> tuple[int, dict[str, object]]:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    identity = NativePostgreSQLIdentity(
        dialect="postgresql",
        driver="psycopg",
        database="disposable",
        server_version="17.10",
        server_version_full="PostgreSQL 17.10 on x86_64-pc-linux-gnu",
        server_address="127.0.0.1",
        server_port=55432,
    )
    inventory = (PASS_NODEID, TEST_NODEID)
    captured: dict[str, object] = {}

    def fake_run(command: list[str], *, env: dict[str, str]):
        report_path = Path(command[command.index("--postgresql-report") + 1])
        skipped = [TEST_NODEID] if skip_reason is not None else []
        executed = [PASS_NODEID] if skip_reason is not None else list(inventory)
        report_path.write_text(
            json.dumps(
                {
                    "identity": identity.as_dict(),
                    "tests": {
                        "selected": len(inventory),
                        "executed": len(executed),
                        "passed": len(executed),
                        "failed": 0,
                        "errors": 0,
                        "skipped": len(skipped),
                        "unavailable": 0,
                        "selected_nodeids": list(inventory),
                        "executed_nodeids": executed,
                        "passed_nodeids": executed,
                        "failed_nodeids": [],
                        "error_nodeids": [],
                        "skipped_nodeids": skipped,
                        "unavailable_nodeids": [],
                        "skip_reasons": (
                            {
                                TEST_NODEID: repr(
                                    (
                                        "/tmp/test_governed_waiver.py",
                                        42,
                                        f"Skipped: {skip_reason}",
                                    )
                                )
                            }
                            if skip_reason is not None
                            else {}
                        ),
                    },
                    "pytest_exit_status": 0,
                }
            ),
            encoding="utf-8",
        )
        return {
            "command": command,
            "exit_code": 0,
            "duration_seconds": 0.0,
            "output": "",
            "output_sha256": "0" * 64,
        }

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "discover_native_postgresql_inventory", lambda _root: inventory)
    monkeypatch.setitem(main.__globals__, "probe_native_postgresql", lambda _dsn: identity)
    monkeypatch.setitem(main.__globals__, "_run", fake_run)
    monkeypatch.setitem(
        main.__globals__,
        "_write_atomic",
        lambda path, value: captured.update(path=path, report=value),
    )
    argv = _cert_args("--output", str(tmp_path / "native.json"))
    for waiver in waivers:
        argv.extend(("--waive-skip", waiver))
    result = main(argv)
    report = captured["report"]
    assert isinstance(report, dict)
    return result, report


def test_postgresql_dsn_has_no_shared_infrastructure_fallback(monkeypatch) -> None:
    """An unset DISH_TEST_POSTGRESQL_DSN must never resolve to a real database.

    A prior default silently pointed at TEST's actual dark-launch database
    (127.0.0.1:55432/dish_stage_a_test). native_migration_database and
    tests/support/postgresql/core.py's _reset_postgresql_schema() DROP and
    re-migrate whatever schema this resolves to, so an unconfigured DSN must
    fail closed via NativePostgreSQLUnavailable, not connect to anything.
    """
    monkeypatch.delenv("DISH_TEST_POSTGRESQL_DSN", raising=False)
    assert postgresql_dsn() is None
    assert redacted_dsn(postgresql_dsn()) == "(DISH_TEST_POSTGRESQL_DSN not set)"
    with pytest.raises(NativePostgreSQLUnavailable):
        probe_native_postgresql()


@pytest.mark.parametrize("database", ("dish_stage_a_test", "dish_stage_a_prod"))
def test_native_postgresql_probe_rejects_live_deployment_database_identity(
    monkeypatch, database: str
) -> None:
    monkeypatch.setattr(
        certification,
        "create_engine",
        lambda *_args, **_kwargs: _probe_engine(database),
    )

    with pytest.raises(
        NativePostgreSQLUnavailable,
        match=rf"live Dish deployment database {database!r}",
    ):
        probe_native_postgresql(
            "postgresql+psycopg://dish:secret@127.0.0.1:55432/dish_test"
        )


@pytest.mark.parametrize("database", ("dish_stage_a_test", "dish_stage_a_prod"))
def test_live_deployment_identity_blocks_reset_and_migration_before_sql(
    monkeypatch, database: str
) -> None:
    import tests.support.postgresql.migrations as migrations

    reset_engine = MagicMock()
    reset_engine.dialect.name = "postgresql"
    upgrade = MagicMock()
    monkeypatch.setattr(
        certification,
        "create_engine",
        lambda *_args, **_kwargs: _probe_engine(database),
    )
    monkeypatch.setattr(migrations, "create_engine", lambda *_args, **_kwargs: reset_engine)
    monkeypatch.setattr(migrations.command, "upgrade", upgrade)
    target = migrations.MigrationDatabase(
        sqlalchemy_url="postgresql+psycopg://dish:secret@127.0.0.1:55432/dish_test",
        expected_dialect="postgresql",
        certification_evidence=True,
        lane="native_postgresql_certification",
    )

    with pytest.raises(NativePostgreSQLUnavailable):
        target.fresh_bootstrap()

    reset_engine.begin.assert_not_called()
    upgrade.assert_not_called()


def test_native_postgresql_probe_accepts_disposable_database_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        certification,
        "create_engine",
        lambda *_args, **_kwargs: _probe_engine("dish_test"),
    )

    identity = probe_native_postgresql(
        "postgresql+psycopg://dish:secret@127.0.0.1:55432/dish_stage_a_test"
    )

    assert identity.database == "dish_test"


def test_native_postgresql_certification_inventory_is_derived_and_nonempty() -> None:
    inventory = discover_native_postgresql_inventory(ROOT)
    assert inventory == tuple(sorted(set(inventory)))
    assert inventory
    assert all(nodeid.startswith("tests/postgresql/native/test_") for nodeid in inventory)
    assert all("::test_" in nodeid for nodeid in inventory)


def test_native_postgresql_selector_refuses_sqlite_backend() -> None:
    from tests.conftest import _select_items

    class Config:
        values = {
            "--smoke": False,
            "--database-boundary": False,
            "--flake-candidates": False,
            "--quarantine": False,
            "--pglite": False,
            "--native-postgresql": True,
            "--postgresql": False,
        }

        def getoption(self, name: str):
            return self.values[name]

    with pytest.raises(pytest.UsageError, match="requires --postgresql"):
        _select_items(Config(), [])


def test_native_certification_reports_unavailable_without_masquerading(
    monkeypatch, tmp_path: Path
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    captured: dict[str, object] = {}

    def unavailable(_dsn: str):
        raise NativePostgreSQLUnavailable("connection refused by disposable test target")

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "probe_native_postgresql", unavailable)
    monkeypatch.setitem(
        main.__globals__,
        "_write_atomic",
        lambda path, value: captured.update(path=path, report=value),
    )
    assert main(_cert_args("--output", str(tmp_path / "native.json"))) == 3
    report = captured["report"]
    assert isinstance(report, dict)
    assert report["status"] == "unavailable"
    assert report["native_postgresql_certified"] is False
    assert report["tests"]["executed"] == 0
    assert report["tests"]["unavailable"] == len(discover_native_postgresql_inventory(ROOT))


def test_local_bootstrap_rejects_stale_head_before_provision(monkeypatch) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    main = namespace["main"]
    monkeypatch.setitem(
        main.__globals__,
        "ensure_canonical_local_postgresql",
        lambda: pytest.fail("PostgreSQL bootstrap must not start"),
    )

    assert main(["--ensure-local-postgresql", "--expected-head", "b" * 40]) == 4


def test_local_bootstrap_ignores_spoofed_primary_root_before_provision(monkeypatch) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    main = namespace["main"]
    protected_primary = execution_guard._protected_primary_root()
    monkeypatch.setenv("DISH_PROTECTED_PRIMARY_ROOT", "/somewhere/else")
    monkeypatch.setattr(execution_guard, "_git", lambda _root, *args: {
        ("rev-parse", "--show-toplevel"): str(protected_primary),
        ("branch", "--show-current"): "",
        ("rev-parse", "HEAD"): CANDIDATE_HEAD,
    }[args])
    monkeypatch.setitem(
        main.__globals__,
        "ensure_canonical_local_postgresql",
        lambda: pytest.fail("PostgreSQL bootstrap must not start"),
    )

    assert main(_cert_args("--ensure-local-postgresql")) == 4


def test_native_certification_focused_selection_reports_only_required_inventory(
    monkeypatch, tmp_path: Path
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    captured: dict[str, object] = {}

    def unavailable(_dsn: str):
        raise NativePostgreSQLUnavailable("focused disposable target unavailable")

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "probe_native_postgresql", unavailable)
    monkeypatch.setitem(
        main.__globals__,
        "_write_atomic",
        lambda path, value: captured.update(path=path, report=value),
    )
    test_file = "tests/postgresql/native/test_migration_status.py"
    assert main(_cert_args("--output", str(tmp_path / "native.json"), "--test-file", test_file)) == 3
    report = captured["report"]
    assert isinstance(report, dict)
    required = [
        nodeid
        for nodeid in discover_native_postgresql_inventory(ROOT)
        if nodeid.split("::", 1)[0] == test_file
    ]
    assert required
    assert report["selection_mode"] == "focused"
    assert report["selected_test_files"] == [test_file]
    assert report["required_inventory"] == required
    assert report["tests"]["unavailable"] == len(required)
    assert len(required) < report["inventory_count"]


def test_native_certification_rejects_zero_executed_tests(
    monkeypatch, tmp_path: Path
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    identity = NativePostgreSQLIdentity(
        dialect="postgresql",
        driver="psycopg",
        database="disposable",
        server_version="17.10",
        server_version_full="PostgreSQL 17.10 on x86_64-pc-linux-gnu",
        server_address="127.0.0.1",
        server_port=55432,
    )
    captured: dict[str, object] = {}

    def fake_run(command: list[str], *, env: dict[str, str]):
        report_path = Path(command[command.index("--postgresql-report") + 1])
        report_path.write_text(
            json.dumps(
                {
                    "identity": identity.as_dict(),
                    "tests": {
                        "selected": len(discover_native_postgresql_inventory(ROOT)),
                        "executed": 0,
                        "passed": 0,
                        "failed": 0,
                        "errors": 0,
                        "skipped": len(discover_native_postgresql_inventory(ROOT)),
                        "unavailable": 0,
                        "selected_nodeids": list(discover_native_postgresql_inventory(ROOT)),
                        "skipped_nodeids": list(discover_native_postgresql_inventory(ROOT)),
                        "skip_reasons": {
                            nodeid: "governed fixture skip"
                            for nodeid in discover_native_postgresql_inventory(ROOT)
                        },
                    },
                    "pytest_exit_status": 0,
                }
            ),
            encoding="utf-8",
        )
        return {
            "command": command,
            "exit_code": 0,
            "duration_seconds": 0.0,
            "output": "",
            "output_sha256": "0" * 64,
        }

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "probe_native_postgresql", lambda _dsn: identity)
    monkeypatch.setitem(main.__globals__, "_run", fake_run)
    monkeypatch.setitem(
        main.__globals__,
        "_write_atomic",
        lambda path, value: captured.update(path=path, report=value),
    )
    assert main(_cert_args("--output", str(tmp_path / "native.json"))) == 2
    report = captured["report"]
    assert isinstance(report, dict)
    assert report["native_postgresql_certified"] is False
    assert report["tests"]["executed"] == 0
    assert report["unwaived_skips"] == sorted(discover_native_postgresql_inventory(ROOT))


def test_native_certification_passes_without_skips_or_waivers(monkeypatch, tmp_path: Path) -> None:
    result, report = _run_fake_certification(monkeypatch, tmp_path)
    assert result == 0
    assert report["native_postgresql_certified"] is True
    assert report["waived_skips"] == []
    assert report["unused_waivers"] == []


def test_native_certification_failure_tail_is_bounded_and_redacted() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    failure_output_tail = namespace["_failure_output_tail"]
    limit = namespace["FAILURE_OUTPUT_TAIL_CHARACTERS"]
    password = "native-test-secret"
    query_secret = "ssl-query-secret"
    dsn = (
        f"postgresql+psycopg://dish:{password}@127.0.0.1:5432/dish_test"
        f"?sslpassword={query_secret}"
    )
    # Place a DSN across the eventual tail boundary. Redaction must happen
    # before truncation so no credential fragment survives at the cutoff.
    output = "x" * (limit - len(dsn) // 2) + dsn + "\ntraceback sentinel\n"

    rendered = failure_output_tail(output, dsn)

    assert len(rendered) <= limit
    assert password not in rendered
    assert query_secret not in rendered
    assert "sslpassword" not in rendered
    assert "postgresql+psycopg://dish:***@127.0.0.1:5432/dish_test" in rendered
    assert rendered.endswith("traceback sentinel\n")


def test_native_certification_accepts_only_matching_structured_skip_waiver(
    monkeypatch, tmp_path: Path
) -> None:
    reason = "governed fixture skip"
    result, report = _run_fake_certification(
        monkeypatch, tmp_path, skip_reason=reason, waivers=(_waiver(TEST_NODEID, reason),)
    )
    assert result == 0
    assert report["native_postgresql_certified"] is True
    assert report["waived_skips"] == [TEST_NODEID]
    assert report["unwaived_skips"] == []
    assert report["waiver_reason_mismatches"] == []


def test_native_certification_fails_on_valid_but_unused_skip_waiver(
    monkeypatch, tmp_path: Path
) -> None:
    result, report = _run_fake_certification(
        monkeypatch,
        tmp_path,
        waivers=(_waiver(TEST_NODEID, "governed fixture skip"),),
    )
    assert result == 2
    assert report["native_postgresql_certified"] is False
    assert report["unused_waivers"] == [TEST_NODEID]


def test_native_certification_fails_when_same_node_skips_for_different_reason(
    monkeypatch, tmp_path: Path
) -> None:
    result, report = _run_fake_certification(
        monkeypatch,
        tmp_path,
        skip_reason="replacement skip reason",
        waivers=(_waiver(TEST_NODEID, "original skip reason"),),
    )
    assert result == 2
    assert report["native_postgresql_certified"] is False
    assert report["unwaived_skips"] == [TEST_NODEID]
    assert report["waiver_reason_mismatches"] == [
        {
            "nodeid": TEST_NODEID,
            "expected_reason_sha256": hashlib.sha256(b"original skip reason").hexdigest(),
            "observed_reason_sha256": hashlib.sha256(b"replacement skip reason").hexdigest(),
        }
    ]


def test_structured_skip_waiver_schema_rejects_malformed_duplicate_and_review_due() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    parse = namespace["_parse_waivers"]
    today = date(2026, 8, 14)
    with pytest.raises(ValueError, match="JSON object"):
        parse([f"{TEST_NODEID}=legacy prose"], today=today)
    waiver = _waiver(TEST_NODEID, "governed fixture skip")
    with pytest.raises(ValueError, match="duplicate skip waiver"):
        parse([waiver, waiver], today=today)
    with pytest.raises(ValueError, match="review-due or expired"):
        parse([_waiver(TEST_NODEID, "governed fixture skip", review_by="2026-08-14")], today=today)


def test_native_certification_rejects_structured_waiver_for_unknown_node(
    monkeypatch, tmp_path: Path
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    main = namespace["main"]
    monkeypatch.setitem(
        main.__globals__,
        "discover_native_postgresql_inventory",
        lambda _root: (TEST_NODEID,),
    )
    unknown = "tests/postgresql/native/test_unknown.py::test_unknown"
    with pytest.raises(SystemExit, match="not in the governed inventory"):
        main(
            _cert_args(
                "--output",
                str(tmp_path / "native.json"),
                "--waive-skip",
                _waiver(unknown, "unknown reason"),
            )
        )


def test_pglite_report_classifies_assertion_and_infrastructure_failures(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-pglite"))
    report = tmp_path / "junit.xml"
    report.write_text(
        """<?xml version='1.0' encoding='utf-8'?>
<testsuite tests='2' failures='1' errors='1' skipped='0'>
  <testcase classname='tests.pglite' name='assertion'><failure message='assert 1 == 2'/></testcase>
  <testcase classname='tests.pglite' name='lifecycle'><error message='server closed the connection unexpectedly'/></testcase>
</testsuite>
""",
        encoding="utf-8",
    )
    summary = namespace["_junit_summary"](report)
    assert summary["assertion_failures"] == 1
    assert summary["infrastructure_failures"] == 1
