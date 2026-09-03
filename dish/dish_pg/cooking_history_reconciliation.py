"""Reconcile Cooking History membership against Dishes already in PostgreSQL."""
import uuid
from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


def reconcile_existing_history(
    session: Session,
    history_gids: Iterable[str],
    cook: Callable[[uuid.UUID], None],
) -> dict[str, list[str]]:
    """Cook existing alias matches; report and ignore unmatched History tasks."""
    gids = tuple(dict.fromkeys(map(str, history_gids)))
    rows = session.execute(
        select(
            models.TaskExternalAlias.external_id,
            models.DishTask.task_id,
            models.DishState.completed,
            models.DishState.completion_reason,
        )
        .join(models.DishTask, models.DishTask.task_id == models.TaskExternalAlias.task_id)
        .join(models.DishState, models.DishState.task_id == models.DishTask.task_id)
        .join(models.AuthorityGeneration, models.AuthorityGeneration.generation_id == models.DishState.generation_id)
        .where(
            models.TaskExternalAlias.external_system == "asana",
            models.TaskExternalAlias.state == "active",
            models.DishTask.existence_state != "retired",
            models.AuthorityGeneration.status == "active",
            models.TaskExternalAlias.external_id.in_(gids),
        )
    ).all()
    found = {gid: (task_id, completed, reason) for gid, task_id, completed, reason in rows}
    report = {name: [] for name in ("matched", "changed", "already_cooked", "unmatched")}
    for gid in gids:
        match = found.get(gid)
        if match is None:
            report["unmatched"].append(gid)
            continue
        report["matched"].append(gid)
        if match[1] and match[2] == "cooked":
            report["already_cooked"].append(gid)
        else:
            cook(match[0])
            report["changed"].append(gid)
    return report
