from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from tests.support.postgresql.certification import (
    discover_native_postgresql_inventory,
    NativePostgreSQLIdentity,
    NativePostgreSQLUnavailable,
    postgresql_dsn,
    probe_native_postgresql,
    redacted_dsn,
)

pytestmark = pytest.mark.smoke
ROOT = Path(__file__).resolve().parents[2]


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
    assert main(["--output", str(tmp_path / "native.json")]) == 3
    report = captured["report"]
    assert isinstance(report, dict)
    assert report["status"] == "unavailable"
    assert report["native_postgresql_certified"] is False
    assert report["tests"]["executed"] == 0
    assert report["tests"]["unavailable"] == len(discover_native_postgresql_inventory(ROOT))


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
    assert main(["--output", str(tmp_path / "native.json"), "--test-file", test_file]) == 3
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
    assert main(["--output", str(tmp_path / "native.json")]) == 2
    report = captured["report"]
    assert isinstance(report, dict)
    assert report["native_postgresql_certified"] is False
    assert report["tests"]["executed"] == 0
    assert report["unwaived_skips"] == sorted(discover_native_postgresql_inventory(ROOT))


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
