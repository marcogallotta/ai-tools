from __future__ import annotations

import ast
import json
import runpy
from pathlib import Path

import pytest

from tests.support.postgresql.certification import (
    NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY,
    NativePostgreSQLIdentity,
    NativePostgreSQLUnavailable,
)

pytestmark = pytest.mark.smoke
ROOT = Path(__file__).resolve().parents[2]


def _native_test_nodeids() -> set[str]:
    nodeids: set[str] = set()
    for path in sorted((ROOT / "tests" / "postgresql" / "native").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(ROOT)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                nodeids.add(f"{relative}::{node.name}")
    return nodeids


def test_native_postgresql_certification_inventory_is_literal_and_complete() -> None:
    assert len(NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY) == len(
        set(NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY)
    )
    assert set(NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY) == _native_test_nodeids()


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
    assert report["tests"]["unavailable"] == len(
        NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY
    )


def test_native_certification_rejects_zero_executed_tests(
    monkeypatch, tmp_path: Path
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    identity = NativePostgreSQLIdentity(
        dialect="postgresql",
        driver="psycopg",
        database="disposable",
        server_version="17.5",
        server_version_full="PostgreSQL 17.5 on x86_64-pc-linux-gnu",
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
                        "selected": len(NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY),
                        "executed": 0,
                        "passed": 0,
                        "failed": 0,
                        "errors": 0,
                        "skipped": len(NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY),
                        "unavailable": 0,
                        "selected_nodeids": list(
                            NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY
                        ),
                        "skipped_nodeids": list(
                            NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY
                        ),
                        "skip_reasons": {
                            nodeid: "governed fixture skip"
                            for nodeid in NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY
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
    assert report["unwaived_skips"] == list(
        NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY
    )


def test_pglite_report_keeps_foundational_quarantine_visible(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-pglite"))
    assert namespace["PGLITE_FOUNDATIONAL_QUARANTINE"] == (
        "tests/postgresql/pglite/test_pglite_migrations.py::test_native_fixture_reset_uses_alembic_history",
    )
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

    deterministic_error = tmp_path / "deterministic-error.xml"
    deterministic_error.write_text(
        """<?xml version='1.0' encoding='utf-8'?>
<testsuite tests='1' failures='0' errors='1' skipped='0'>
  <testcase classname='tests.pglite' name='deterministic'><error message='AssertionError: expected revision 0027, got 0018'/></testcase>
</testsuite>
""",
        encoding="utf-8",
    )
    deterministic_summary = namespace["_junit_summary"](deterministic_error)
    assert deterministic_summary["assertion_failures"] == 1
    assert deterministic_summary["infrastructure_failures"] == 0

    quarantine = tmp_path / "quarantine.xml"
    quarantine.write_text(
        """<?xml version='1.0' encoding='utf-8'?>
<testsuite tests='2' failures='0' errors='0' skipped='0'>
  <testcase classname='tests.postgresql.pglite.test_pglite_migrations' name='test_native_fixture_reset_uses_alembic_history'/>
  <testcase classname='tests.postgresql.pglite.test_pglite_bootstrap' name='test_pglite_starts_and_reports_postgresql_version'/>
</testsuite>
""",
        encoding="utf-8",
    )
    quarantine_summary = namespace["_junit_summary"](quarantine)
    assert namespace["_required_inventory_present"](
        quarantine_summary, namespace["PGLITE_FOUNDATIONAL_QUARANTINE"]
    )
