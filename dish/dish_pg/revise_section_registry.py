"""One-shot operational trigger for the correction-ImportRun registry-role revision.

``revise-section-registry`` is a retained admin command (see
``command_port.py``) with no legacy equivalent, so it can never be reached
through ordinary shadow replay. This module invokes it directly against a
PostgreSQL target through ``PostgresCommandPort``, the same execution path
the live service and shadow worker use, outside of any legacy-originated
request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import models
from .bootstrap import require_postgresql_target
from .command_port import CommandCall, CommandResult, PostgresCommandPort
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .workflow import WorkflowAuthorityService


class ReviseSectionRegistryError(ValueError):
    """The target, arguments, or result of the registry-role correction is unsafe."""


def revise_section_registry(
    session,
    *,
    research_queue_section_id: uuid.UUID,
    verification_queue_section_id: uuid.UUID,
    owner_id: str,
    agent: str,
    cursor_secret: bytes,
    now: datetime,
    uuid_factory=uuid.uuid4,
) -> CommandResult:
    if research_queue_section_id == verification_queue_section_id:
        raise ReviseSectionRegistryError(
            "Research Queue and Verification Queue must be different sections"
        )
    generation_id = session.scalar(
        select(models.AuthorityGeneration.generation_id).where(
            models.AuthorityGeneration.status == "active"
        )
    )
    if generation_id is None:
        raise ReviseSectionRegistryError("no active authority generation")
    run_id = uuid_factory()
    capability_digest = hashlib.sha256(
        f"registry-role-correction:{owner_id}:{agent}:{run_id}:{now.isoformat()}".encode()
    ).digest()
    WorkflowAuthorityService(session, uuid_factory=uuid_factory).register_run(
        run_id=run_id,
        generation_id=generation_id,
        owner_id=owner_id,
        agent=agent,
        capability_digest=capability_digest,
        registered_at=now,
    )
    port = PostgresCommandPort(session, cursor_secret=cursor_secret, uuid_factory=uuid_factory)
    return port.execute(
        CommandCall(
            command_name="revise-section-registry",
            arguments={
                "research_queue_section_id": str(research_queue_section_id),
                "verification_queue_section_id": str(verification_queue_section_id),
            },
            owner_id=owner_id,
            principal_class="admin",
            run_id=run_id,
            request_id=uuid_factory(),
            now=now,
        )
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(value, handle, sort_keys=True, indent=2, default=str)
            handle.write("\n")
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-revise-section-registry")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", required=True)
    parser.add_argument("--schema-head", required=True)
    parser.add_argument("--research-queue-section-id", type=uuid.UUID, required=True)
    parser.add_argument("--verification-queue-section-id", type=uuid.UUID, required=True)
    parser.add_argument("--owner-id", default="Marco")
    parser.add_argument(
        "--agent",
        default="marco",
        choices=("claude", "gpt", "codex", "marco", "service"),
    )
    parser.add_argument("--cursor-secret-env", default="DISH_PG_CURSOR_SECRET")
    parser.add_argument("--receipt", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cursor_secret = os.environ.get(args.cursor_secret_env, "").encode()
    if len(cursor_secret) < 24:
        print(
            json.dumps(
                {"error": f"{args.cursor_secret_env} must be set to at least 24 bytes"}
            )
        )
        return 2
    try:
        engine = create_database_engine(DatabaseSettings(url=args.database_url))
        try:
            require_postgresql_target(
                engine,
                expected_database_name=args.expected_database_name,
                schema_head=args.schema_head,
            )
            factory = session_factory(engine)
            now = datetime.now(timezone.utc)
            with session_scope(factory) as session:
                result = revise_section_registry(
                    session,
                    research_queue_section_id=args.research_queue_section_id,
                    verification_queue_section_id=args.verification_queue_section_id,
                    owner_id=args.owner_id,
                    agent=args.agent,
                    cursor_secret=cursor_secret,
                    now=now,
                )
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - reported verbatim to the operator
        report = {"error": str(exc), "type": type(exc).__name__}
        if args.receipt is not None:
            _atomic_json(args.receipt, report)
        print(json.dumps(report, sort_keys=True))
        return 2
    receipt = {
        "ok": result.ok,
        "command": result.command,
        "code": result.code,
        "http_status": result.http_status,
        "data": dict(result.data),
    }
    if args.receipt is not None:
        _atomic_json(args.receipt, receipt)
    print(json.dumps(receipt, sort_keys=True, indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
