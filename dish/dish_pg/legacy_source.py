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


def export_legacy_source(*, database: Path, location_manifest: Path, output: Path) -> int:
    locations = _manifest(location_manifest)
    uri=f"file:{database.expanduser().resolve()}?mode=ro"
    conn=sqlite3.connect(uri, uri=True)
    conn.row_factory=sqlite3.Row
    try:
        heads=conn.execute(
            """SELECT task_gid,last_confirmed_identity,last_confirmed_title,
                      last_confirmed_notes,schema_version,confirmed_at
                 FROM task_content_state ORDER BY task_gid"""
        ).fetchall()
    finally:
        conn.close()
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
                "completed": bool(location["completed"]),
                "observed_at": str(location["observed_at"]),
                "existence_state": str(location.get("existence_state", "ordinary")),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise LegacySourceError(f"invalid location manifest task={row['task_gid']}: {exc}") from exc
        lines.append(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
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
