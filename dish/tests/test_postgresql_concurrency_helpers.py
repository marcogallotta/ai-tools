from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import runpy
from pathlib import Path
import pytest
from sqlalchemy import create_engine, text

from dish_tool.errors import DishRuleError

ROOT = Path(__file__).resolve().parents[1]
from tests.support.postgresql.concurrency import (
    TransactionGate,
    TransactionOutcome,
    assert_conditional_update_contention_lost,
    assert_lease_takeover,
    assert_stale_writer_rejected,
    assert_transaction_aborted,
    assert_transaction_blocked,
    assert_transaction_committed,
    execute_transaction,
    independent_connections,
)


def test_transaction_gate_proves_blocked_until_explicit_release() -> None:
    gate = TransactionGate(label="unit gate")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(gate.block)
        gate.wait_until_blocked()
        assert_transaction_blocked(future)
        gate.release()
        assert future.result(timeout=1) is None


def test_transaction_outcome_assertions_distinguish_commit_and_abort() -> None:
    committed = TransactionOutcome(value="ok", error=None, committed=True, rolled_back=False)
    assert assert_transaction_committed(committed) == "ok"

    error = ValueError("bad")
    aborted = TransactionOutcome(value=None, error=error, committed=False, rolled_back=True)
    assert assert_transaction_aborted(aborted, error_type=ValueError) is error


def test_stale_writer_assertion_requires_exact_rule() -> None:
    error = DishRuleError("CONFLICT", "stale", rule="stale_writer")
    outcome = TransactionOutcome(value=None, error=error, committed=False, rolled_back=True)
    assert assert_stale_writer_rejected(outcome, expected_rule="stale_writer") is error
    with pytest.raises(AssertionError, match="expected stale-writer rule"):
        assert_stale_writer_rejected(outcome, expected_rule="other")


def test_lease_takeover_and_conditional_update_assertions() -> None:
    before = {
        "lease_id": "old",
        "owner_id": "owner-a",
        "run_id": "run-a",
        "released_at": "2026-08-04T18:00:00+00:00",
    }
    after = {
        "lease_id": "new",
        "owner_id": "owner-b",
        "run_id": "run-b",
        "released_at": None,
    }
    assert_lease_takeover(
        before,
        after,
        expected_owner_id="owner-b",
        expected_run_id="run-b",
    )
    assert_conditional_update_contention_lost(0)
    with pytest.raises(AssertionError, match="rowcount 0"):
        assert_conditional_update_contention_lost(1)

def test_independent_transactions_report_commit_and_rollback(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'transactions.sqlite3'}", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE events(value TEXT NOT NULL)"))
        with independent_connections(engine) as (first, second):
            committed = execute_transaction(
                first, lambda session: session.execute(text("INSERT INTO events VALUES ('ok')"))
            )

            def reject(session):
                session.execute(text("INSERT INTO events VALUES ('rolled-back')"))
                raise ValueError("reject transaction")

            aborted = execute_transaction(second, reject)
        assert assert_transaction_committed(committed).rowcount == 1
        assert isinstance(assert_transaction_aborted(aborted, error_type=ValueError), ValueError)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT value FROM events")).scalars().all() == ["ok"]
    finally:
        engine.dispose()




def test_probe_canonical_local_postgresql_accepts_cidr_form_server_address(monkeypatch) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    globals_ = namespace["_probe_canonical_local_postgresql"].__globals__

    class FakeIdentity:
        database = "dish_test"
        server_port = 5432
        server_address = "127.0.0.1/32"

        def as_dict(self):
            return {
                "database": self.database,
                "server_port": self.server_port,
                "server_address": self.server_address,
            }

    monkeypatch.setitem(globals_, "probe_native_postgresql", lambda dsn: FakeIdentity())
    monkeypatch.setitem(
        globals_,
        "_probe_canonical_local_role_capabilities",
        lambda: {"role": "dish_test", "createdb": True, "createrole": True},
    )
    result = namespace["_probe_canonical_local_postgresql"]()
    assert result["role"] == "dish_test"
    assert result["createdb"] is True
    assert result["createrole"] is True


@pytest.mark.parametrize(
    ("database", "server_address", "server_port", "message"),
    [
        ("not_dish_test", "127.0.0.1/32", 5432, "identity mismatch"),
        ("dish_test", "10.0.0.25/32", 5432, "not loopback"),
        ("dish_test", "127.0.0.1/32", 6543, "identity mismatch"),
    ],
)
def test_probe_canonical_local_postgresql_refuses_noncanonical_target_before_role_probe(
    monkeypatch, database, server_address, server_port, message
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    unavailable = namespace["LocalPostgreSQLUnavailable"]
    globals_ = namespace["_probe_canonical_local_postgresql"].__globals__

    class FakeIdentity:
        def as_dict(self):
            return {
                "database": database,
                "server_port": server_port,
                "server_address": server_address,
            }

    FakeIdentity.database = database
    FakeIdentity.server_port = server_port
    FakeIdentity.server_address = server_address
    monkeypatch.setitem(globals_, "probe_native_postgresql", lambda dsn: FakeIdentity())
    monkeypatch.setitem(
        globals_,
        "_probe_canonical_local_role_capabilities",
        lambda: pytest.fail("noncanonical target must be rejected before role capability probing"),
    )

    with pytest.raises(unavailable, match=message):
        namespace["_probe_canonical_local_postgresql"]()


def test_probe_canonical_local_role_requires_createdb_and_createrole(monkeypatch) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    unavailable = namespace["LocalPostgreSQLUnavailable"]
    globals_ = namespace["_probe_canonical_local_role_capabilities"].__globals__

    class FakeResult:
        def __init__(self, row):
            self.row = row

        def mappings(self):
            return self

        def one(self):
            return self.row

    class FakeConnection:
        def __init__(self, row):
            self.row = row

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, statement):
            assert "rolcreatedb" in str(statement)
            assert "rolcreaterole" in str(statement)
            return FakeResult(self.row)

    class FakeEngine:
        def __init__(self, row):
            self.row = row
            self.disposed = False

        def connect(self):
            return FakeConnection(self.row)

        def dispose(self):
            self.disposed = True

    def probe(row):
        engine = FakeEngine(row)
        monkeypatch.setitem(globals_, "create_engine", lambda *args, **kwargs: engine)
        return engine, namespace["_probe_canonical_local_role_capabilities"]

    engine, probe_capabilities = probe(
        {"role": "dish_test", "createdb": True, "createrole": True}
    )
    assert probe_capabilities() == {
        "role": "dish_test",
        "createdb": True,
        "createrole": True,
    }
    assert engine.disposed is True

    engine, probe_capabilities = probe(
        {"role": "dish_test", "createdb": True, "createrole": False}
    )
    with pytest.raises(unavailable, match="CREATEROLE"):
        probe_capabilities()
    assert engine.disposed is True


def test_canonical_local_postgresql_bootstrap_is_bounded_and_reports_residual_reason(monkeypatch) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    unavailable = namespace["LocalPostgreSQLUnavailable"]
    globals_ = namespace["ensure_canonical_local_postgresql"].__globals__
    identity = {
        "database": "dish_test",
        "role": "dish_test",
        "server_address": "127.0.0.1",
        "server_port": 5432,
        "createdb": True,
        "createrole": True,
    }

    monkeypatch.setitem(globals_, "_probe_canonical_local_postgresql", lambda: identity)
    monkeypatch.setitem(globals_, "_provision_canonical_local_postgresql", lambda: pytest.fail("reachable target must not reset"))
    assert namespace["ensure_canonical_local_postgresql"]()["source"] == "existing"

    probes = iter([unavailable("canonical local role capabilities missing: CREATEROLE"), identity])
    provisioned: list[bool] = []
    def probe():
        value = next(probes)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setitem(globals_, "_probe_canonical_local_postgresql", probe)
    monkeypatch.setitem(globals_, "_require_canonical_local_server", lambda: None)
    monkeypatch.setitem(globals_, "_provision_canonical_local_postgresql", lambda: provisioned.append(True))
    assert namespace["ensure_canonical_local_postgresql"]()["source"] == "provisioned"
    assert provisioned == [True]

    monkeypatch.setitem(globals_, "_probe_canonical_local_postgresql", lambda: (_ for _ in ()).throw(unavailable("missing")))
    monkeypatch.setitem(globals_, "_provision_canonical_local_postgresql", lambda: (_ for _ in ()).throw(unavailable("sudo refused")))
    assert namespace["ensure_canonical_local_postgresql"]()["reason"] == "sudo refused"

    monkeypatch.setattr(namespace["shutil"], "which", lambda name: f"/usr/bin/{name}")
    class NoServer:
        returncode = 2
        stdout = "localhost:5432 - no response"
        stderr = ""
    monkeypatch.setattr(namespace["subprocess"], "run", lambda *args, **kwargs: NoServer())
    with pytest.raises(unavailable, match="server unavailable at localhost:5432"):
        namespace["_require_canonical_local_server"]()

    monkeypatch.setenv("PGHOST", "remote")
    calls = []
    class SudoDenied:
        returncode = 1
        stdout = ""
        stderr = "sudo: a password is required"
    monkeypatch.setattr(namespace["subprocess"], "run", lambda command, **kwargs: (calls.append((command, kwargs)) or SudoDenied()))
    with pytest.raises(unavailable, match="noninteractive privilege unavailable"):
        namespace["_provision_canonical_local_postgresql"]()
    command, kwargs = calls[0]
    assert command[1:4] == ["-n", "-u", "postgres"]
    assert "-w" in command and kwargs["timeout"] == 10
    assert "PGHOST" not in kwargs["env"]
    assert any("CREATEDB CREATEROLE" in part for part in command)


def test_native_concurrency_lane_is_explicit_first_and_local_helper_is_canonical_only(monkeypatch) -> None:
    lane = runpy.run_path(str(ROOT / "scripts" / "dish-test-lane"))
    explicit = "postgresql+psycopg://dish_test:explicit@127.0.0.1:6543/isolated"
    monkeypatch.setattr(lane["subprocess"], "run", lambda *args, **kwargs: pytest.fail("explicit target must bypass helper"))
    assert lane["_bootstrap_native_postgresql_env"]({"DISH_TEST_POSTGRESQL_DSN": explicit})["DISH_TEST_POSTGRESQL_DSN"] == explicit
    assert lane["_bootstrap_native_postgresql_env"]({"DISH_PG_TEST_URL": explicit})["DISH_TEST_POSTGRESQL_DSN"] == explicit

    fixed = "postgresql+psycopg://dish_test:0ddca88b81a8bf1a15d84caa78efd7b3@localhost:5432/dish_test"
    class Ready:
        returncode = 0
        stdout = json.dumps({"status": "ready", "dsn": fixed})
        stderr = ""
    monkeypatch.setattr(lane["subprocess"], "run", lambda *args, **kwargs: Ready())
    assert lane["_bootstrap_native_postgresql_env"]({}) == {"DISH_TEST_POSTGRESQL_DSN": fixed}

    class Bad:
        returncode = 0
        stdout = json.dumps({"status": "ready", "dsn": "postgresql://other@remote/PROD"})
        stderr = ""
    monkeypatch.setattr(lane["subprocess"], "run", lambda *args, **kwargs: Bad())
    with pytest.raises(RuntimeError, match="failed closed"):
        lane["_bootstrap_native_postgresql_env"]({})

    helper = runpy.run_path(str(ROOT / "scripts" / "dish-pg-native-certification"))
    monkeypatch.setitem(helper["main"].__globals__, "ensure_canonical_local_postgresql", lambda: pytest.fail("parser must reject target injection first"))
    for args in (
        ["--ensure-local-postgresql", "--dsn", "postgresql://other@remote/TEST"],
        ["--ensure-local-postgresql", "--output", "x"],
    ):
        with pytest.raises(SystemExit) as exc:
            helper["main"](args)
        assert exc.value.code == 2
