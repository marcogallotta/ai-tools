from __future__ import annotations

import sqlite3

import pytest

from dish_tool.transactions import (
    immediate_transaction,
    join_or_begin_immediate,
    require_transaction,
    savepoint_transaction,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE facts(value TEXT NOT NULL)")
    return conn


def _values(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT value FROM facts ORDER BY rowid")]


def test_immediate_transaction_commits_owned_unit():
    conn = _connection()

    with immediate_transaction(conn, "commit"):
        conn.execute("INSERT INTO facts(value) VALUES('committed')")

    assert not conn.in_transaction
    assert _values(conn) == ["committed"]


@pytest.mark.smoke
@pytest.mark.invariant_transaction_rollback
@pytest.mark.parametrize("error", [RuntimeError("failure"), SystemExit("crash")])
def test_immediate_transaction_rolls_back_errors_and_process_exit(error):
    conn = _connection()

    with pytest.raises(type(error)):
        with immediate_transaction(conn, "rollback"):
            conn.execute("INSERT INTO facts(value) VALUES('rolled-back')")
            raise error

    assert not conn.in_transaction
    assert _values(conn) == []


def test_nested_immediate_transaction_uses_isolated_savepoint():
    conn = _connection()

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO facts(value) VALUES('outer-before')")
    with pytest.raises(RuntimeError):
        with immediate_transaction(conn, "nested"):
            conn.execute("INSERT INTO facts(value) VALUES('inner')")
            raise RuntimeError("rollback nested unit")
    conn.execute("INSERT INTO facts(value) VALUES('outer-after')")

    assert conn.in_transaction
    assert _values(conn) == ["outer-before", "outer-after"]
    conn.execute("COMMIT")


def test_savepoint_transaction_preserves_outer_unit_on_process_exit():
    conn = _connection()

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO facts(value) VALUES('outer')")
    with pytest.raises(SystemExit):
        with savepoint_transaction(conn, "nested-crash"):
            conn.execute("INSERT INTO facts(value) VALUES('inner')")
            raise SystemExit("crash")

    assert conn.in_transaction
    assert _values(conn) == ["outer"]
    conn.execute("ROLLBACK")


def test_join_or_begin_commits_only_when_it_owns_the_transaction():
    conn = _connection()

    with join_or_begin_immediate(conn, "owned"):
        conn.execute("INSERT INTO facts(value) VALUES('owned')")
    assert not conn.in_transaction

    conn.execute("BEGIN IMMEDIATE")
    with join_or_begin_immediate(conn, "joined"):
        conn.execute("INSERT INTO facts(value) VALUES('joined')")
    assert conn.in_transaction
    conn.execute("ROLLBACK")

    assert _values(conn) == ["owned"]


def test_joined_transaction_leaves_rollback_authority_with_caller():
    conn = _connection()

    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError):
        with join_or_begin_immediate(conn, "joined-error"):
            conn.execute("INSERT INTO facts(value) VALUES('joined')")
            raise RuntimeError("caller decides")

    assert conn.in_transaction
    assert _values(conn) == ["joined"]
    conn.execute("ROLLBACK")
    assert _values(conn) == []


def test_require_transaction_rejects_unowned_use():
    conn = _connection()

    with pytest.raises(RuntimeError, match="completion requires an active caller transaction"):
        require_transaction(conn, operation="completion")

    conn.execute("BEGIN IMMEDIATE")
    require_transaction(conn, operation="completion")
    conn.execute("ROLLBACK")


def test_transaction_control_sql_is_centralized():
    """Only the transaction primitive module may own SQLite control statements."""

    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for package in (root / "dish_tool", root / "dish_service"):
        for path in package.glob("*.py"):
            if path.name == "transactions.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                if not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
                    continue
                statement = node.args[0]
                if not isinstance(statement, ast.Constant) or not isinstance(statement.value, str):
                    continue
                control = statement.value.strip().upper()
                if control.startswith(("BEGIN ", "COMMIT", "ROLLBACK", "SAVEPOINT ", "RELEASE ")):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}:{control}")

    assert offenders == []
