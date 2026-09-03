"""Lean Cooking History import/reconciliation helpers."""
from __future__ import annotations
import json, sqlite3, tempfile, uuid
from datetime import datetime
from pathlib import Path
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from . import models, stage3_models as wf
from .legacy_source import export_legacy_source
from .location_manifest import target_uuid
from .repositories import CoreAuthorityError, DishRepository, ScalarMutationSource


def export_history_isolated(*, database: Path, location_manifest: Path, history_snapshot: Path, output: Path) -> int:
    manifest = json.loads(location_manifest.read_text())
    tasks = manifest.get("tasks") if isinstance(manifest, dict) else None
    snapshot = json.loads(history_snapshot.read_text())
    rows = snapshot.get("tasks") if isinstance(snapshot, dict) else None
    if not isinstance(tasks, dict) or not isinstance(rows, list):
        raise CoreAuthorityError("Cooking History source evidence is malformed")
    history = {str(r["gid"]): r["completed"] for r in rows if isinstance(r, dict) and str(r.get("gid", "")).isdigit() and isinstance(r.get("completed"), bool)}
    conn = sqlite3.connect(f"file:{database.expanduser().resolve()}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
    try:
        heads = conn.execute("SELECT task_gid,confirmed_at FROM task_content_state ORDER BY task_gid").fetchall()
    finally:
        conn.close()
    for row in heads:
        gid = str(row["task_gid"])
        if gid not in tasks and gid in history:
            tasks[gid] = {"task_id": str(target_uuid("task", gid)), "project_ids": [], "section_id": None,
                          "section_gid": None, "section_name": None, "completed": history[gid],
                          "observed_at": str(row["confirmed_at"]), "existence_state": "isolated"}
    with tempfile.TemporaryDirectory() as tmp:
        augmented = Path(tmp) / "locations.json"; augmented.write_text(json.dumps({"tasks": tasks}, sort_keys=True))
        return export_legacy_source(database=database, location_manifest=augmented, output=output, allow_departed_tasks=True)


def reclassify_imported_isolated(session: Session, *, generation_id: uuid.UUID, task_id: uuid.UUID, execution_id: uuid.UUID, at: datetime) -> int:
    task = session.get(models.DishTask, task_id); state = session.get(models.DishState, (generation_id, task_id)); fence = session.get(wf.TaskExecutionFence, execution_id)
    if task is None or state is None or fence is None or task.creation_route != "import" or task.existence_state != "isolated":
        raise CoreAuthorityError("Cooking History reconciliation authority is incomplete")
    if state.section_id is not None or state.archived_at is not None or state.completion_reason != "imported":
        raise CoreAuthorityError("Cooking History imported baseline does not match")
    members = session.scalar(select(func.count()).select_from(models.CurrentTaskProjectMembership).where(models.CurrentTaskProjectMembership.generation_id == generation_id, models.CurrentTaskProjectMembership.task_id == task_id, models.CurrentTaskProjectMembership.is_member.is_(True)))
    if members or fence.generation_id != generation_id or fence.task_id != task_id:
        raise CoreAuthorityError("Cooking History reconciliation fence/membership mismatch")
    mutation = DishRepository(session).begin_scalar_mutation(generation_id=generation_id, task_id=task_id, expected_dish_version=fence.expected_dish_version,
        expected_membership_revision=fence.expected_membership_revision, source=ScalarMutationSource(route="command_execution", command_execution_id=execution_id, occurred_at=at))
    mutation.set_completion(completed=True, reason="cooked"); result = mutation.finalize()
    if result.dish_version is None: raise CoreAuthorityError("Cooking History reconciliation produced no mutation")
    return result.dish_version
