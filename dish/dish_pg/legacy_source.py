"""Deterministic SQLite-to-importer source export for dark launch.

The exporter performs no Asana reads. A separately captured location manifest
must supply the target stable identities, placement, completion, and observation
time for every SQLite task head.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping


class LegacySourceError(ValueError):
    pass


def _manifest(path: Path) -> dict[str, Mapping[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    tasks = value.get("tasks") if isinstance(value, dict) else None
    if not isinstance(tasks, dict):
        raise LegacySourceError("location manifest must contain a tasks object")
    return {str(key): item for key, item in tasks.items() if isinstance(item, Mapping)}


def _atomic_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            for line in lines:
                handle.write(line + "\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd=os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(temp): os.unlink(temp)


_OPERATION_HISTORY_TABLES = {"operations", "service_leases", "verification_cycles"}


def _canonical_uuid(value: object, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise LegacySourceError(f"{field} is not a UUID: {value!r}") from exc


def _require_operation_history_tables(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = sorted(_OPERATION_HISTORY_TABLES - tables)
    if missing:
        raise LegacySourceError(
            "legacy source is missing required operation-history tables: " + ", ".join(missing)
        )


def _terminal_operation_history(conn: sqlite3.Connection, *, task_gid: str) -> dict[str, object]:
    operations = conn.execute(
        """SELECT operation_id,operation_kind,status,phase,terminal_outcome,created_at,completed_at
             FROM operations WHERE task_gid=? ORDER BY created_at,operation_id""",
        (task_gid,),
    ).fetchall()
    operation_ids = {str(row["operation_id"]) for row in operations}
    operation_records: list[dict[str, object]] = []
    for row in operations:
        if row["status"] not in {"completed", "cancelled"}:
            raise LegacySourceError(
                "operation-history export admits only terminal completed/cancelled operations: "
                f"task_gid={task_gid} operation_id={row['operation_id']} status={row['status']}"
            )
        if row["phase"] != "terminal" or row["completed_at"] is None or not str(row["terminal_outcome"] or "").strip():
            raise LegacySourceError(
                "terminal legacy operation has inconsistent terminal evidence: "
                f"task_gid={task_gid} operation_id={row['operation_id']}"
            )
        operation_records.append({
            "operation_id": _canonical_uuid(row["operation_id"], field="operation_id"),
            "kind": str(row["operation_kind"]),
            "status": str(row["status"]),
            "phase": str(row["phase"]),
            "terminal_outcome": str(row["terminal_outcome"]),
            "created_at": str(row["created_at"]),
            "completed_at": str(row["completed_at"]),
        })

    cycles = conn.execute(
        """SELECT cycle_id,operation_id,cycle_number,outcome,created_at,completed_at
             FROM verification_cycles WHERE task_gid=? ORDER BY created_at,cycle_id""",
        (task_gid,),
    ).fetchall()
    cycle_by_id: dict[str, sqlite3.Row] = {}
    cycle_records: list[dict[str, object]] = []
    for row in cycles:
        if str(row["operation_id"]) not in operation_ids:
            raise LegacySourceError(
                f"verification cycle references operation outside task history: cycle_id={row['cycle_id']}"
            )
        if row["completed_at"] is None or row["outcome"] is None:
            raise LegacySourceError(
                "operation-history export does not admit open verification cycles: "
                f"task_gid={task_gid} cycle_id={row['cycle_id']}"
            )
        cycle_by_id[str(row["cycle_id"])] = row
        cycle_records.append({
            "cycle_id": _canonical_uuid(row["cycle_id"], field="cycle_id"),
            "operation_id": _canonical_uuid(row["operation_id"], field="cycle.operation_id"),
            "cycle_sequence": int(row["cycle_number"]),
            "outcome": str(row["outcome"]),
            "created_at": str(row["created_at"]),
            "completed_at": str(row["completed_at"]),
        })

    leases = conn.execute(
        """SELECT lease_id,operation_id,owner_id,run_id,lease_kind,actor_attempt_seq,
                  context_cycle_id,acquired_at,expires_at,released_at
             FROM service_leases WHERE task_gid=? ORDER BY acquired_at,lease_id""",
        (task_gid,),
    ).fetchall()
    lease_records: list[dict[str, object]] = []
    for row in leases:
        lease_kind = str(row["lease_kind"] or "")
        if lease_kind not in {"actor", "admin_request"}:
            raise LegacySourceError(
                f"legacy lease lacks supported lease_kind: task_gid={task_gid} lease_id={row['lease_id']}"
            )
        if str(row["operation_id"]) not in operation_ids:
            raise LegacySourceError(
                f"legacy lease references operation outside task history: lease_id={row['lease_id']}"
            )
        if row["released_at"] is None:
            raise LegacySourceError(
                "operation-history export does not admit active service leases: "
                f"task_gid={task_gid} lease_id={row['lease_id']}"
            )
        if lease_kind == "actor" and row["actor_attempt_seq"] is None:
            raise LegacySourceError(
                f"legacy actor lease lacks attempt sequence: task_gid={task_gid} lease_id={row['lease_id']}"
            )
        if lease_kind == "admin_request" and row["actor_attempt_seq"] is not None:
            raise LegacySourceError(
                f"legacy admin lease carries actor attempt sequence: lease_id={row['lease_id']}"
            )
        context_cycle_id = row["context_cycle_id"]
        if context_cycle_id is not None:
            cycle = cycle_by_id.get(str(context_cycle_id))
            if cycle is None or str(cycle["operation_id"]) != str(row["operation_id"]):
                raise LegacySourceError(
                    f"legacy lease verification cycle does not belong to its operation: lease_id={row['lease_id']}"
                )
        source_run_id = str(row["run_id"] or "").strip()
        if not source_run_id:
            raise LegacySourceError(f"legacy lease lacks run_id: lease_id={row['lease_id']}")
        lease_records.append({
            "lease_id": _canonical_uuid(row["lease_id"], field="lease_id"),
            "operation_id": _canonical_uuid(row["operation_id"], field="lease.operation_id"),
            "source_run_id": source_run_id,
            "owner_id": str(row["owner_id"]),
            "lease_kind": lease_kind,
            "actor_attempt_sequence": row["actor_attempt_seq"],
            "verification_cycle_id": (
                _canonical_uuid(context_cycle_id, field="lease.context_cycle_id")
                if context_cycle_id is not None else None
            ),
            "issued_at": str(row["acquired_at"]),
            "expires_at": str(row["expires_at"]),
            "released_at": str(row["released_at"]),
        })

    return {
        "operations": operation_records,
        "leases": lease_records,
        "verification_cycles": cycle_records,
    }


def export_legacy_source(*, database: Path, location_manifest: Path, output: Path) -> int:
    locations = _manifest(location_manifest)
    uri=f"file:{database.expanduser().resolve()}?mode=ro"
    conn=sqlite3.connect(uri, uri=True)
    conn.row_factory=sqlite3.Row
    try:
        _require_operation_history_tables(conn)
        heads=conn.execute(
            """SELECT task_gid,last_confirmed_identity,last_confirmed_title,
                      last_confirmed_notes,schema_version,confirmed_at
                 FROM task_content_state ORDER BY task_gid"""
        ).fetchall()
        observed={row["task_gid"] for row in heads}
        missing=sorted(observed-set(locations))
        extras=sorted(set(locations)-observed)
        if missing or extras:
            raise LegacySourceError(
                f"location manifest corpus mismatch missing={missing} extras={extras}"
            )
        lines=[]
        for row in heads:
            location=locations[row["task_gid"]]
            try:
                record={
                    "task_id": str(uuid.UUID(str(location["task_id"]))),
                    "asana_task_gid": row["task_gid"],
                    "title": row["last_confirmed_title"],
                    "body": row["last_confirmed_notes"],
                    "identity_scheme": row["schema_version"],
                    "content_identity": row["last_confirmed_identity"],
                    "project_ids": [str(uuid.UUID(str(value))) for value in location["project_ids"]],
                    "section_id": str(uuid.UUID(str(location["section_id"]))),
                    "section_gid": str(location["section_gid"]),
                    "completed": bool(location["completed"]),
                    "observed_at": str(location["observed_at"]),
                    "existence_state": str(location.get("existence_state", "ordinary")),
                    "operation_history": _terminal_operation_history(conn, task_gid=row["task_gid"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise LegacySourceError(f"invalid location manifest task={row['task_gid']}: {exc}") from exc
            lines.append(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    finally:
        conn.close()
    _atomic_lines(output, lines)
    return len(lines)


def _parser() -> argparse.ArgumentParser:
    parser=argparse.ArgumentParser(prog="dish-pg-export-legacy")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--location-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None=None) -> int:
    args=_parser().parse_args(argv)
    count=export_legacy_source(database=args.database, location_manifest=args.location_manifest, output=args.output)
    print(json.dumps({"exported": count, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
