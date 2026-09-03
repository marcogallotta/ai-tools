import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .command_port import CommandCall, PostgresCommandPort
from .database import DatabaseSettings, create_database_engine
from .workflow import WorkflowAuthorityService


def reconcile_existing_history(
    session: Session, history_gids: list[str], *, cursor_secret: bytes
) -> dict[str, list[str]]:
    gids = tuple(dict.fromkeys(map(str, history_gids)))
    alias, task, state, generation = models.TaskExternalAlias, models.DishTask, models.DishState, models.AuthorityGeneration
    rows = session.execute(
        select(alias.external_id, task.task_id, state.completed, state.completion_reason)
        .join(task, task.task_id == alias.task_id).join(state, state.task_id == task.task_id)
        .join(generation, generation.generation_id == state.generation_id)
        .where(alias.external_system == "asana", alias.state == "active",
               task.existence_state != "retired", generation.status == "active",
               alias.external_id.in_(gids))).all()
    found = {gid: values for gid, *values in rows}
    report = {key: [] for key in ("matched", "changed", "already_cooked", "unmatched")}
    pending = [values[0] for values in found.values() if not (values[1] and values[2] == "cooked")]
    if pending:
        now, run_id = datetime.now(timezone.utc), uuid.uuid4()
        generation_id = session.scalar(select(generation.generation_id).where(generation.status == "active"))
        WorkflowAuthorityService(session).register_run(
            run_id=run_id, generation_id=generation_id, owner_id="cooking-history-cutover",
            agent="service", capability_digest=hashlib.sha256(run_id.bytes).digest(),
            registered_at=now)
        port = PostgresCommandPort(session, cursor_secret=cursor_secret)
        for task_id in pending:
            result = port.execute(CommandCall("cooked", {"task_id": str(task_id)},
                "cooking-history-cutover", "agent", run_id, uuid.uuid4(), now))
            if not result.ok:
                raise RuntimeError(f"cooked authority rejected {task_id}: {result.code}")
    for gid in gids:
        values = found.get(gid)
        if values is None:
            report["unmatched"].append(gid)
        else:
            report["matched"].append(gid)
            report["already_cooked" if values[1] and values[2] == "cooked" else "changed"].append(gid)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="dish-pg-reconcile-cooking-history")
    parser.add_argument("gid", nargs="+")
    args = parser.parse_args()
    engine = create_database_engine(DatabaseSettings(url=os.environ["DISH_PG_URL"]))
    with Session(engine) as session, session.begin():
        report = reconcile_existing_history(session, args.gid, cursor_secret=os.environ["DISH_PG_CURSOR_SECRET"].encode())
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
