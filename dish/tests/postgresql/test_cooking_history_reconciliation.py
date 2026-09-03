from sqlalchemy import select

from dish_pg import models
from dish_pg.cooking_history_reconciliation import reconcile_existing_history
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
