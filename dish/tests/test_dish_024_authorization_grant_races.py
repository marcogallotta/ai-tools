import sqlite3
import threading

import pytest

from dish_tool.admin import DishAdminApplication
from dish_tool.database import (
    record_marco_authorization,
    reserve_marco_authorizations,
    transition_operation,
)
from dish_tool.errors import DishRuleError
from test_dish_tool_step7_verification import make_app


def _second_connection(conn):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
    other.row_factory = sqlite3.Row
    other.execute("PRAGMA foreign_keys=ON")
    return other


def _grant(conn, operation_id):
    return record_marco_authorization(
        conn,
        task_gid="t",
        operation_id=operation_id,
        field_name="Purpose",
        before="old",
        after="new",
        reason="Marco approved the exact Purpose change",
        actor_run_id="marco-run",
    )


def test_duplicate_exact_grant_race_creates_one_audited_capability(tmp_path):
    app, _, operation_id, _ = make_app(tmp_path)
    first = _second_connection(app.conn)
    second = _second_connection(app.conn)
    barrier = threading.Barrier(2)
    rows = []
    errors = []

    def worker(conn):
        try:
            barrier.wait(timeout=5)
            rows.append(_grant(conn, operation_id))
        except Exception as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(first,)), threading.Thread(target=worker, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(rows) == 2
    assert rows[0]["authorization_id"] == rows[1]["authorization_id"]
    assert app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE consumed_at IS NULL"
    ).fetchone()[0] == 1
    assert app.conn.execute(
        """SELECT COUNT(*) FROM audit_events
             WHERE event_type='marco.authorization'
               AND json_extract(details, '$.authorization_id')=?""",
        (rows[0]["authorization_id"],),
    ).fetchone()[0] == 1
    first.close()
    second.close()


def test_terminalization_between_admin_precheck_and_atomic_grant_is_rejected(tmp_path, monkeypatch):
    app, backend, operation_id, _ = make_app(tmp_path)
    grant_conn = _second_connection(app.conn)
    admin = DishAdminApplication(
        grant_conn, backend=backend, release_loader=lambda: app._load_release("verification")
    )
    terminal = _second_connection(app.conn)
    entered = threading.Event()
    resume = threading.Event()
    original = record_marco_authorization

    def paused(*args, **kwargs):
        entered.set()
        assert resume.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr("dish_tool.database.record_marco_authorization", paused)
    result = {}

    def grant():
        result.update(
            admin.execute(
                "authorize-governed-change",
                submission_id=operation_id,
                field="Purpose",
                before="old",
                after="new",
                reason="Marco approved the exact Purpose change",
                run_id="marco-run",
            )
        )

    thread = threading.Thread(target=grant)
    thread.start()
    assert entered.wait(timeout=5)
    transition_operation(
        terminal,
        operation_id,
        phase="terminal",
        status="cancelled",
        terminal_outcome="discarded",
    )
    resume.set()
    thread.join(timeout=10)

    assert result["code"] == "WRONG_STATE"
    assert result["errors"][0]["rule"] == "authorization_operation_not_open"
    assert app.conn.execute("SELECT COUNT(*) FROM marco_authorizations").fetchone()[0] == 0
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='marco.authorization'"
    ).fetchone()[0] == 0
    terminal.close()
    grant_conn.close()


@pytest.mark.smoke
@pytest.mark.invariant_authorization
def test_authorization_is_not_reservable_before_grant_audit_commits(tmp_path, monkeypatch):
    app, _, operation_id, _ = make_app(tmp_path)
    grant_conn = _second_connection(app.conn)
    reserve_conn = _second_connection(app.conn)
    audit_entered = threading.Event()
    release_audit = threading.Event()
    reserve_started = threading.Event()
    reserve_finished = threading.Event()
    thread_errors: list[BaseException] = []
    original = __import__("dish_tool.database", fromlist=["record_audit"]).record_audit

    def paused_audit(conn, **kwargs):
        if kwargs.get("event_type") == "marco.authorization":
            audit_entered.set()
            assert release_audit.wait(timeout=5)
        return original(conn, **kwargs)

    monkeypatch.setattr("dish_tool.database.record_audit", paused_audit)
    grant_result = []
    reserve_result = []

    grant_thread = threading.Thread(target=lambda: grant_result.append(_grant(grant_conn, operation_id)))
    grant_thread.start()
    assert audit_entered.wait(timeout=5)

    # The row is uncommitted and therefore not externally visible or usable.
    assert app.conn.execute("SELECT COUNT(*) FROM marco_authorizations").fetchone()[0] == 0

    def reserve():
        reserve_started.set()
        try:
            reserve_result.extend(
                reserve_marco_authorizations(
                    reserve_conn,
                    task_gid="t",
                    operation_id=operation_id,
                    changes=({"field": "Purpose", "before": "old", "after": "new"},),
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            thread_errors.append(exc)
        finally:
            reserve_finished.set()

    reserve_thread = threading.Thread(target=reserve, name="authorization-reserver")
    reserve_thread.start()
    assert reserve_started.wait(timeout=5)
    assert not reserve_finished.is_set(), "reservation completed before the grant audit committed"

    release_audit.set()
    grant_thread.join(timeout=10)
    reserve_thread.join(timeout=10)
    assert not grant_thread.is_alive()
    assert not reserve_thread.is_alive()
    assert thread_errors == []
    assert len(grant_result) == 1
    assert len(reserve_result) == 1
    assert reserve_result[0]["authorization_id"] == grant_result[0]["authorization_id"]
    grant_conn.close()
    reserve_conn.close()


def test_grant_audit_failure_rolls_back_capability(tmp_path, monkeypatch):
    app, _, operation_id, _ = make_app(tmp_path)

    def fail_audit(conn, **kwargs):
        if kwargs.get("event_type") == "marco.authorization":
            raise sqlite3.OperationalError("injected grant audit failure")
        raise AssertionError("unexpected audit")

    monkeypatch.setattr("dish_tool.database.record_audit", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="injected grant audit failure"):
        _grant(app.conn, operation_id)

    assert app.conn.execute("SELECT COUNT(*) FROM marco_authorizations").fetchone()[0] == 0
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='marco.authorization'"
    ).fetchone()[0] == 0


def test_historical_unaudited_authorization_is_inert(tmp_path):
    app, _, operation_id, _ = make_app(tmp_path)
    app.conn.execute(
        """INSERT INTO marco_authorizations(
               authorization_id,task_gid,operation_id,field_name,before_json,after_json,
               reason,created_at
           ) VALUES('legacy-unaudited','t',?,'Purpose','\"old\"','\"new\"','legacy',datetime('now'))""",
        (operation_id,),
    )
    with pytest.raises(DishRuleError) as exc:
        reserve_marco_authorizations(
            app.conn,
            task_gid="t",
            operation_id=operation_id,
            changes=({"field": "Purpose", "before": "old", "after": "new"},),
        )
    assert exc.value.rule == "governed_change_unauthorized"
    row = app.conn.execute(
        "SELECT reserved_by_operation_id,consumed_at FROM marco_authorizations WHERE authorization_id='legacy-unaudited'"
    ).fetchone()
    assert tuple(row) == (None, None)
