"""Fail-open legacy completion capture for the PostgreSQL dark launch.

The live SQLite/Asana request remains authoritative. Capture failures are
written to an owner-only emergency ledger and never replace the live result.
No Asana client is constructed or called from this module.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from dish_shadow.policy import treatment_for

from .shadow_spool import EmergencyGapWriter, ShadowSpool

LOGGER = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _rows(conn: sqlite3.Connection, table: str, where: str, values: tuple[Any, ...]) -> list[dict[str, Any]]:
    try:
        result = conn.execute(
            f'SELECT * FROM "{table}" WHERE {where} ORDER BY rowid', values
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [{key: _jsonable(row[key]) for key in row.keys()} for row in result]


def authoritative_snapshot(
    db_path: Path,
    *,
    arguments: Mapping[str, Any],
    request_id: str | None,
    busy_timeout_ms: int = 50,
) -> dict[str, Any]:
    """Capture a bounded SQLite-only authority snapshot for one command.

    Rows are selected only by the command's task, operation, and request
    identities. This deliberately does not perform an independent Asana read.
    """
    task_gid = str(arguments.get("task_gid") or "").strip() or None
    operation_id = str(
        arguments.get("submission_id") or arguments.get("operation_id") or ""
    ).strip() or None
    uri = f"file:{Path(db_path).expanduser().resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        snapshot: dict[str, Any] = {
            "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "task_gid": task_gid,
            "operation_id": operation_id,
            "request_id": request_id,
            "tables": {},
        }
        selectors: list[tuple[str, str, tuple[Any, ...]]] = []
        if task_gid:
            for table in (
                "task_content_state", "operations", "submissions", "service_leases",
                "dish_inspect_facts", "planning_reopen_attempts",
            ):
                selectors.append((table, "task_gid=?", (task_gid,)))
        if operation_id:
            for table in (
                "operations", "content_versions", "verification_cycles", "write_attempts",
                "movement_attempts", "operation_steps", "operation_actor_facts", "audit_events",
                "marco_authorizations", "operation_executions", "operation_execution_claims",
                "abandonment_attempts", "operation_successions",
            ):
                selectors.append((table, "operation_id=?", (operation_id,)))
        if request_id:
            for table in ("service_requests", "backup_creations"):
                selectors.append((table, "request_id=?", (request_id,)))
        seen: set[tuple[str, str, tuple[Any, ...]]] = set()
        for selector in selectors:
            if selector in seen or selector[0] not in tables:
                continue
            seen.add(selector)
            rows = _rows(conn, *selector)
            if rows:
                snapshot["tables"].setdefault(selector[0], []).extend(rows)
        return snapshot
    finally:
        conn.close()


@dataclass(frozen=True)
class ShadowCaptureSettings:
    mode: str
    spool_path: Path
    emergency_dir: Path
    source_authority_generation: str
    kill_switch_path: Path | None = None
    busy_timeout_ms: int = 50

    @property
    def enabled(self) -> bool:
        return self.mode in {"capture", "execute"} and not (
            self.kill_switch_path is not None and self.kill_switch_path.exists()
        )


class LegacyShadowCapture:
    """Mirror a completed legacy request without becoming request authority."""

    def __init__(self, settings: ShadowCaptureSettings, *, db_path: Path) -> None:
        self.settings = settings
        self.db_path = Path(db_path)
        self.spool = ShadowSpool(
            settings.spool_path, busy_timeout_ms=settings.busy_timeout_ms
        )
        self.emergency = EmergencyGapWriter(settings.emergency_dir)
        if settings.enabled:
            try:
                self.spool.initialize()
            except BaseException:
                LOGGER.exception("dark-launch spool initialization failed; capture remains fail-open")

    @staticmethod
    def _principal(principal: Any) -> dict[str, Any]:
        if principal is None:
            return {}
        return {
            "owner_id": getattr(principal, "owner_id", None),
            "run_id": getattr(principal, "run_id", None),
            "principal_class": getattr(principal, "principal_class", None),
        }

    def _emergency(self, *, identity: str, command: str, phase: str, error: BaseException) -> None:
        try:
            self.emergency.write(
                identity=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                payload={
                    "source_request_identity": identity,
                    "command": command,
                    "phase": phase,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "recorded_at": _utcnow().isoformat(),
                }
            )
        except BaseException:
            LOGGER.exception("dark-launch emergency-gap write failed")

    def execute(
        self,
        *,
        command: str,
        arguments: Mapping[str, Any],
        principal: Any,
        request_id: str | None,
        call: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            return call()
        try:
            treatment = treatment_for(command)
        except ValueError:
            LOGGER.warning("dark-launch command has no treatment; capture bypassed", extra={"command": command})
            return call()
        if treatment.treatment == "excluded":
            return call()
        identity = str(request_id or f"unreplayed:{uuid.uuid4()}")
        canonical_input = {"command": command, "arguments": _jsonable(arguments)}
        principal_payload = self._principal(principal)
        try:
            existing = self.spool.get_by_source_identity(identity)
            if existing is not None and existing.state in {"complete", "gap", "delivered"}:
                return call()
            pre_state = authoritative_snapshot(
                self.db_path,
                arguments=arguments,
                request_id=request_id,
                busy_timeout_ms=self.settings.busy_timeout_ms,
            )
            reservation = self.spool.reserve(
                source_request_identity=identity,
                source_authority_generation=self.settings.source_authority_generation,
                command_name=command,
                treatment=treatment.treatment,
                canonical_input=canonical_input,
                principal=principal_payload,
                source_pre_state=pre_state,
                pinned_inputs={"capture_schema": 1, "rollout_mode": self.settings.mode},
                created_at=_utcnow(),
            )
        except BaseException as exc:
            self._emergency(identity=identity, command=command, phase="reserve", error=exc)
            return call()

        try:
            result = call()
        except BaseException as exc:
            try:
                self.spool.record_gap(
                    reservation.registration_id,
                    gap={
                        "classification": "legacy_call_raised",
                        "missing_evidence": ["source_outcome", "source_post_state"],
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                    completed_at=_utcnow(),
                )
            except BaseException:
                self._emergency(identity=identity, command=command, phase="legacy_call", error=exc)
            raise
        try:
            post_state = authoritative_snapshot(
                self.db_path,
                arguments=arguments,
                request_id=request_id,
                busy_timeout_ms=self.settings.busy_timeout_ms,
            )
            effects = {
                "legacy_result_sha256": hashlib.sha256(
                    json.dumps(_jsonable(result), sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "external_effects_observed_independently": False,
            }
            self.spool.complete(
                reservation.registration_id,
                source_outcome=_jsonable(result),
                source_post_state=post_state,
                source_effects=effects,
                completed_at=_utcnow(),
            )
        except BaseException as exc:
            try:
                self.spool.record_gap(
                    reservation.registration_id,
                    gap={
                        "classification": "capture_completion_failed",
                        "missing_evidence": ["source_post_state"],
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                    completed_at=_utcnow(),
                )
            except BaseException:
                self._emergency(identity=identity, command=command, phase="complete", error=exc)
        return result
