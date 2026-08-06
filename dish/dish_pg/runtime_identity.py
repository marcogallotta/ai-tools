"""Exact TEST-runtime identity assertions shared by PostgreSQL processes."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from . import models
from .database import session_scope


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def write_json_atomic(path: Path, value: object) -> None:
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(canonical_json_bytes(value))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def assert_runtime_identity(
    session_maker,
    *,
    role: str,
    expected_database: str,
    expected_schema_head: str,
    expected_release: str,
    expected_generation_id: uuid.UUID,
) -> dict[str, Any]:
    """Query and assert the exact database, migration, release, and generation."""

    with session_scope(session_maker) as session:
        database_name = str(session.scalar(text("SELECT current_database()")))
        schema_head = str(session.scalar(text("SELECT version_num FROM alembic_version")))
        generation = session.scalar(
            select(models.AuthorityGeneration).where(
                models.AuthorityGeneration.status == "active"
            )
        )
        if generation is None:
            raise RuntimeError("PostgreSQL authority has no active generation")
    observed = {
        "database": database_name,
        "schema_head": schema_head,
        "dish_release": generation.dish_release,
        "generation_id": str(generation.generation_id),
        "generation_status": generation.status,
    }
    expected = {
        "database": expected_database,
        "schema_head": expected_schema_head,
        "dish_release": expected_release,
        "generation_id": str(expected_generation_id),
        "generation_status": "active",
    }
    if observed != expected:
        raise RuntimeError(
            f"{role} PostgreSQL runtime identity mismatch: expected={expected!r} observed={observed!r}"
        )
    report = {
        "format": "dish-postgresql-runtime-identity-v1",
        "ok": True,
        "role": role,
        "pid": os.getpid(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "identity": observed,
    }
    report["report_sha256"] = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    return report


def add_runtime_identity_arguments(parser) -> None:
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-schema-head")
    parser.add_argument("--expected-release")
    parser.add_argument("--expected-generation-id", type=uuid.UUID)
    parser.add_argument("--runtime-identity-output", type=Path)


def verify_optional_runtime_identity(args, session_maker, *, role: str) -> dict[str, Any] | None:
    values = (
        args.expected_database,
        args.expected_schema_head,
        args.expected_release,
        args.expected_generation_id,
        args.runtime_identity_output,
    )
    if not any(value is not None for value in values):
        return None
    if not all(value is not None for value in values):
        raise ValueError("runtime identity arguments must be supplied together")
    expected_database = str(args.expected_database)
    if (
        not expected_database.startswith("dish_")
        or not expected_database.endswith("_test")
        or "prod" in expected_database.lower()
        or "production" in expected_database.lower()
    ):
        raise ValueError("runtime identity database must be a disposable dish_*_test database")
    report = assert_runtime_identity(
        session_maker,
        role=role,
        expected_database=expected_database,
        expected_schema_head=str(args.expected_schema_head),
        expected_release=str(args.expected_release),
        expected_generation_id=args.expected_generation_id,
    )
    write_json_atomic(args.runtime_identity_output, report)
    return report
