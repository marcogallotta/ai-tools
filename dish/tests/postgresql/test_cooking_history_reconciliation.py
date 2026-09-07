from __future__ import annotations

import sys
from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import models
import dish_pg.cooking_history_reconciliation as reconciliation
from dish_pg.cooking_history_reconciliation import migrate_missing_history, reconcile_existing_history
from dish_pg.database import session_scope
from tests.support.postgresql.command import _port
from tests.support.postgresql.workflow import NOW


def test_reconciles_only_existing_alias_matches_and_is_rerunnable(workflow_db):
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        _port(session, ids)
        call = lambda: reconcile_existing_history(session, ["123456789", "404"], cursor_secret=b"x" * 32)
        first, second = call(), call()

        assert first == {"matched": ["123456789"], "changed": ["123456789"],
                         "already_cooked": [], "unmatched": ["404"]}
        assert second == {"matched": ["123456789"], "changed": [],
                          "already_cooked": ["123456789"], "unmatched": ["404"]}
        alias = session.scalar(select(models.TaskExternalAlias))
        alias.state, alias.retired_at = "retired", NOW
        session.flush()
        assert call()["unmatched"] == ["123456789", "404"]
        alias.state, alias.retired_at = "active", None
        session.get(models.AuthorityGeneration, context["generation_id"]).status = "pending"
        session.flush()
        assert call()["unmatched"] == ["123456789", "404"]


def test_one_off_history_migration_is_retired_before_database_access():
    class NoDatabaseAccess:
        def __getattr__(self, name: str):
            raise AssertionError(f"retired importer touched database attribute {name}")

    with pytest.raises(RuntimeError, match="one-off Cooking History import is retired"):
        migrate_missing_history(
            NoDatabaseAccess(),
            [{"asana_task_gid": "222222222"}],
            source_sha256="f" * 64,
            cursor_secret=b"x" * 32,
            expected_count=1,
            now=NOW,
        )


def test_one_off_history_cli_is_retired_before_database_connection(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cooking-history-reconciliation", "--import-file", "history.jsonl"])

    def unexpected_database_connection(*args, **kwargs):
        raise AssertionError("retired importer attempted to create a database engine")

    monkeypatch.setattr(reconciliation, "create_database_engine", unexpected_database_connection)

    with pytest.raises(SystemExit) as exc_info:
        reconciliation.main()

    assert exc_info.value.code == 2
    assert "one-off Cooking History import is retired" in capsys.readouterr().err
