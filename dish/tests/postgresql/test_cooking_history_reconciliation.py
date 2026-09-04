from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import models
from dish_pg.cooking_history_reconciliation import migrate_missing_history, reconcile_existing_history
from dish_pg.database import session_scope
from dish_pg.frontend_board_query import FrontendBoardQuery
from dish_service.frontend_board import FrontendBoardConfig, FrontendBoardService
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity
from dish_tool.identifiers import stable_dish_uuid_for_asana_identity
from tests.support.postgresql.command import _port
from tests.support.postgresql.workflow import NOW


def _record(gid: str, title: str) -> dict:
    body = f"{title}\n\nHistorical cook notes."
    return {
        "task_id": str(stable_dish_uuid_for_asana_identity("task", gid)),
        "asana_task_gid": gid,
        "title": title,
        "body": body,
        "identity_scheme": CONTENT_IDENTITY_SCHEME,
        "content_identity": content_identity(title, body),
        "project_ids": ["00000000-0000-0000-0000-000000000001"],
        "section_id": "00000000-0000-0000-0000-000000000002",
        "completed": False,
        "existence_state": "ordinary",
        "observed_at": NOW.isoformat(),
    }


def _hash_rows(session, *, task_id=None) -> str:
    payload = []
    for table in sorted(models.Base.metadata.tables.values(), key=lambda table: table.name):
        if task_id is not None and "task_id" not in table.c:
            continue
        stmt = select(table)
        if task_id is not None:
            stmt = stmt.where(table.c.task_id == task_id)
        rows = [dict(row) for row in session.execute(stmt).mappings()]
        payload.append((table.name, sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _cooked_service(session) -> FrontendBoardService:
    return FrontendBoardService(
        FrontendBoardQuery(session), environment="test", token_secret=b"c" * 32,
        config=FrontendBoardConfig(first_page_size=50, continuation_page_size=50, max_sections=100, projection_delay=timedelta(minutes=15)),
    )


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


def test_one_off_history_migration_is_cooked_isolated_and_idempotent(workflow_db):
    factory, ids, context, existing_task_id = workflow_db
    records = [_record("222222222", "First history dish"), _record("333333333", "Second history dish")]
    with session_scope(factory) as session:
        _port(session, ids)
        protected_hash = _hash_rows(session, task_id=existing_task_id)
        first = migrate_missing_history(session, records, source_sha256="f" * 64, cursor_secret=b"x" * 32, expected_count=2, now=NOW)
        assert first == {"input": 2, "created": 2, "already_present": 0}
        assert _hash_rows(session, task_id=existing_task_id) == protected_hash
        for record in records:
            task_id = stable_dish_uuid_for_asana_identity("task", record["asana_task_gid"])
            task = session.get(models.DishTask, task_id)
            state = session.get(models.DishState, (context["generation_id"], task_id))
            head = session.get(models.TaskMembershipHead, (context["generation_id"], task_id))
            assert task.existence_state == "isolated"
            assert (state.completed, state.completion_reason, state.section_id) == (True, "cooked", None)
            assert head.membership_revision == 0
            assert session.scalars(select(models.CurrentTaskProjectMembership).where(models.CurrentTaskProjectMembership.task_id == task_id)).all() == []
        assert {dish["title"] for dish in _cooked_service(session).archive()["dishes"]} >= {record["title"] for record in records}
        board_titles = {card["title"] for section in _cooked_service(session).bootstrap()["sections"] for card in section["cards"]}
        assert board_titles.isdisjoint({record["title"] for record in records})
        before_rerun = _hash_rows(session)
        second = migrate_missing_history(session, records, source_sha256="f" * 64, cursor_secret=b"x" * 32, expected_count=2, now=NOW)
        assert second == {"input": 2, "created": 0, "already_present": 2}
        assert _hash_rows(session) == before_rerun


def test_one_off_history_migration_rolls_back_before_cooked(monkeypatch, workflow_db):
    factory, ids, _context, existing_task_id = workflow_db
    records = [_record("444444444", "Rollback dish")]
    with session_scope(factory) as session:
        _port(session, ids)
        protected_hash = _hash_rows(session, task_id=existing_task_id)
    monkeypatch.setattr("dish_pg.cooking_history_reconciliation.reconcile_existing_history", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("forced")))
    with pytest.raises(RuntimeError, match="forced"):
        with session_scope(factory) as session:
            migrate_missing_history(session, records, source_sha256="e" * 64, cursor_secret=b"x" * 32, expected_count=1, now=NOW)
    with session_scope(factory) as session:
        assert session.scalar(select(models.TaskExternalAlias).where(models.TaskExternalAlias.external_id == "444444444")) is None
        assert _hash_rows(session, task_id=existing_task_id) == protected_hash
