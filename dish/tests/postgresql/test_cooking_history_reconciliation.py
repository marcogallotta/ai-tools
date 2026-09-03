from dish_pg.cooking_history_reconciliation import reconcile_existing_history
from dish_pg.database import session_scope
from tests.support.postgresql.command import _call, _port
from tests.support.postgresql.workflow import _next, _register_run


def test_reconciles_only_existing_alias_matches_and_is_rerunnable(workflow_db):
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)

        def cook(existing_task_id):
            result = port.execute(_call(
                "cooked", run_id=run_id, request_id=_next(ids),
                arguments={"task_id": str(existing_task_id)},
            ))
            assert result.ok

        first = reconcile_existing_history(session, ["123456789", "404"], cook)
        second = reconcile_existing_history(session, ["123456789", "404"], cook)

        assert first == {"matched": ["123456789"], "changed": ["123456789"],
                         "already_cooked": [], "unmatched": ["404"]}
        assert second == {"matched": ["123456789"], "changed": [],
                          "already_cooked": ["123456789"], "unmatched": ["404"]}
