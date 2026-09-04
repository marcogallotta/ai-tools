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
from .repositories import AuthorityRepository, RegistryRepository, TaskRepository
from .workflow import WorkflowAuthorityService
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity
from dish_tool.identifiers import stable_dish_uuid_for_asana_identity


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


HISTORY_IMPORT_KIND = "cooking-history-one-off-v1"

def migrate_missing_history(session: Session, records: list[dict], *, source_sha256: str, cursor_secret: bytes, expected_count: int = 237, now: datetime | None = None) -> dict[str, int]:
    """Create the bounded missing Cooking History corpus and cook it atomically."""
    now = now or datetime.now(timezone.utc)
    if len(records) != expected_count:
        raise RuntimeError(f"expected {expected_count} Cooking History records, got {len(records)}")
    gids = [str(row.get("asana_task_gid", "")) for row in records]
    if len(set(gids)) != len(gids):
        raise RuntimeError("Cooking History input contains duplicate Asana GIDs")

    authority = AuthorityRepository(session)
    generation = authority.active_generation()
    if generation is None:
        raise RuntimeError("Cooking History migration requires one active authority generation")
    contract = RegistryRepository(session).active_release_contract(generation.generation_id)
    task_repo = TaskRepository(session)
    missing: list[dict] = []
    for row in records:
        gid = str(row.get("asana_task_gid", ""))
        task_id = uuid.UUID(str(row.get("task_id", "")))
        title, body = str(row.get("title", "")), str(row.get("body", ""))
        if task_id != stable_dish_uuid_for_asana_identity("task", gid):
            raise RuntimeError(f"stable Dish identity mismatch for Asana GID {gid}")
        if row.get("identity_scheme") != CONTENT_IDENTITY_SCHEME or row.get("content_identity") != content_identity(title, body):
            raise RuntimeError(f"content identity mismatch for Asana GID {gid}")
        alias = session.scalar(select(models.TaskExternalAlias).where(models.TaskExternalAlias.external_system == "asana", models.TaskExternalAlias.external_id == gid))
        task = session.get(models.DishTask, task_id)
        if alias is None and task is None:
            missing.append(row); continue
        state = session.get(models.DishState, (generation.generation_id, task_id))
        version = None if state is None else session.get(models.ContentVersion, state.current_content_version_id)
        memberships = session.scalars(select(models.CurrentTaskProjectMembership).where(models.CurrentTaskProjectMembership.generation_id == generation.generation_id, models.CurrentTaskProjectMembership.task_id == task_id)).all()
        exact = alias and task and alias.task_id == task_id and alias.state == "active" and task.existence_state == "isolated"
        exact = exact and state and state.completed and state.completion_reason == "cooked" and state.section_id is None and state.archived_at is None and not memberships
        exact = exact and version and (version.title, version.body, version.identity_scheme, version.content_identity) == (title, body, CONTENT_IDENTITY_SCHEME, row["content_identity"])
        if not exact:
            raise RuntimeError(f"existing Dish conflicts with Cooking History input for Asana GID {gid}")

    if missing:
        import_run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"dish:{HISTORY_IMPORT_KIND}:{source_sha256}")
        run = session.get(models.ImportRun, import_run_id)
        if run is None:
            run = models.ImportRun(import_run_id=import_run_id, source_commit=source_sha256, source_release=HISTORY_IMPORT_KIND,
                legacy_generation_id=HISTORY_IMPORT_KIND, baseline_high_water_mark=source_sha256, source_bundle_sha256=source_sha256,
                status="complete", started_at=now, completed_at=now, provenance={"import_kind": HISTORY_IMPORT_KIND, "source_record_count": len(records)})
            authority.add_import_run(run)
        for row in missing:
            gid, task_id = str(row["asana_task_gid"]), uuid.UUID(str(row["task_id"]))
            observed = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
            version_id = uuid.uuid4()
            task_repo.add_imported_task_bundle(
                task=models.DishTask(task_id=task_id, existence_state="isolated", creation_route="import", import_run_id=import_run_id, command_execution_id=None, created_at=observed, retired_at=None),
                alias=models.TaskExternalAlias(alias_id=uuid.uuid4(), task_id=task_id, external_system="asana", external_id=gid, origin="imported", import_run_id=import_run_id, projection_event_id=None, state="active", created_at=observed, retired_at=None),
                receipt=models.DishMutationReceipt(generation_id=generation.generation_id, task_id=task_id, dish_version=1, source_route="import", import_run_id=import_run_id, command_execution_id=None, content_changed=True, placement_changed=True, completion_changed=True, archive_changed=False, occurred_at=observed),
                version=models.ContentVersion(content_version_id=version_id, generation_id=generation.generation_id, task_id=task_id, representation_kind="document", title=str(row["title"]), body=str(row["body"]), identity_scheme=CONTENT_IDENTITY_SCHEME, content_identity=str(row["content_identity"]), creator_route="import", import_run_id=import_run_id, command_execution_id=None, predecessor_content_version_id=None, contract_binding_id=contract.honest_binding.binding_id, created_dish_version=1, created_at=observed),
                state=models.DishState(generation_id=generation.generation_id, task_id=task_id, current_content_version_id=version_id, section_id=None, registry_version_id=contract.active_registry.registry_version_id, completed=False, completion_reason="imported", dish_version=1, placement_version=1, completion_version=1, updated_at=observed),
                membership_head=models.TaskMembershipHead(generation_id=generation.generation_id, task_id=task_id, membership_revision=0, updated_at=observed),
                membership_events=(), current_memberships=(),
            )
    reconcile_existing_history(session, gids, cursor_secret=cursor_secret)
    return {"input": len(records), "created": len(missing), "already_present": len(records) - len(missing)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--import-file")
    parser.add_argument("gid", nargs="*")
    args = parser.parse_args()
    engine = create_database_engine(DatabaseSettings(url=os.environ["DISH_PG_URL"]))
    with Session(engine) as session, session.begin():
        if args.import_file:
            raw = open(args.import_file, "rb").read()
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
            report = migrate_missing_history(session, records, source_sha256=hashlib.sha256(raw).hexdigest(), cursor_secret=os.environ["DISH_PG_CURSOR_SECRET"].encode())
        else:
            report = reconcile_existing_history(session, args.gid, cursor_secret=os.environ["DISH_PG_CURSOR_SECRET"].encode())
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
