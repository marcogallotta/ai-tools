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

from .path_safety import engage_kill_switch, require_distinct_paths
from .shadow_spool import (
    EmergencyGapWriter,
    ShadowSpool,
    ShadowSpoolCapacityError,
)

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
    result = conn.execute(
        f'SELECT * FROM "{table}" WHERE {where} ORDER BY rowid', values
    ).fetchall()
    return [{key: _jsonable(row[key]) for key in row.keys()} for row in result]


def authoritative_snapshot(
    db_path: Path,
    *,
    arguments: Mapping[str, Any],
    request_id: str | None,
    result: Mapping[str, Any] | None = None,
    busy_timeout_ms: int = 50,
) -> dict[str, Any]:
    """Capture bounded SQLite lineage needed for live operation translation.

    The closure follows operation succession relationships so a captured
    successor operation also carries the predecessor operation and the complete
    set of source requests linked to either operation.  It deliberately does
    not perform an independent Asana read or choose a creator request.
    """
    sources = [arguments]
    if isinstance(result, Mapping):
        sources.append(result)
        if isinstance(result.get("data"), Mapping):
            sources.append(result["data"])
    task_gids = {
        str(source.get("task_gid") or "").strip()
        for source in sources
        if str(source.get("task_gid") or "").strip()
    }
    operation_keys = (
        "submission_id", "operation_id", "existing_submission_id",
        "prepared_operation_id", "successor_operation_id",
        "continuation_operation_id", "source_operation_id", "target_operation_id",
    )
    requested_operation_ids = {
        str(source.get(key) or "").strip()
        for source in sources
        for key in operation_keys
        if str(source.get(key) or "").strip()
    }
    operation_ids = set(requested_operation_ids)
    explicit_request_ids = {str(request_id).strip()} if request_id else set()
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

        changed = True
        while changed:
            before = (frozenset(task_gids), frozenset(operation_ids))
            if "operations" in tables:
                for candidate in tuple(operation_ids):
                    for row in _rows(conn, "operations", "operation_id=?", (candidate,)):
                        task_gid = str(row.get("task_gid") or "").strip()
                        if task_gid:
                            task_gids.add(task_gid)
            if "operation_successions" in tables:
                for candidate in tuple(operation_ids):
                    for row in _rows(
                        conn,
                        "operation_successions",
                        "source_operation_id=? OR successor_operation_id=?",
                        (candidate, candidate),
                    ):
                        task_gid = str(row.get("task_gid") or "").strip()
                        if task_gid:
                            task_gids.add(task_gid)
                        for key in ("source_operation_id", "successor_operation_id"):
                            value = str(row.get(key) or "").strip()
                            if value:
                                operation_ids.add(value)
            changed = before != (frozenset(task_gids), frozenset(operation_ids))

        task_gids_sorted = sorted(task_gids)
        operation_ids_sorted = sorted(operation_ids)
        requested_operation_ids_sorted = sorted(requested_operation_ids)
        explicit_request_ids_sorted = sorted(explicit_request_ids)
        snapshot: dict[str, Any] = {
            "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "task_gid": task_gids_sorted[0] if task_gids_sorted else None,
            "operation_id": (
                requested_operation_ids_sorted[0]
                if requested_operation_ids_sorted
                else (operation_ids_sorted[0] if operation_ids_sorted else None)
            ),
            "task_gids": task_gids_sorted,
            "operation_ids": operation_ids_sorted,
            "request_id": request_id,
            "lineage_scope": {
                "operation_ids": operation_ids_sorted,
                "explicit_request_ids": explicit_request_ids_sorted,
            },
            "selected_tables": [],
            "tables": {},
        }
        selectors: list[tuple[str, str, tuple[Any, ...]]] = []
        for task_gid in task_gids_sorted:
            for table in (
                "task_content_state", "operations", "submissions", "service_leases",
                "dish_inspect_facts", "planning_reopen_attempts",
            ):
                selectors.append((table, "task_gid=?", (task_gid,)))
        for operation_id in operation_ids_sorted:
            for table in (
                "operations", "content_versions", "verification_cycles", "write_attempts",
                "movement_attempts", "operation_steps", "operation_actor_facts", "audit_events",
                "marco_authorizations", "operation_executions", "operation_execution_claims",
                "service_requests",
            ):
                selectors.append((table, "operation_id=?", (operation_id,)))
            selectors.append((
                "abandonment_attempts",
                "source_operation_id=? OR successor_operation_id=? OR continuation_operation_id=?",
                (operation_id, operation_id, operation_id),
            ))
            selectors.append((
                "operation_successions",
                "source_operation_id=? OR successor_operation_id=?",
                (operation_id, operation_id),
            ))
        for explicit_request_id in explicit_request_ids_sorted:
            for table in ("service_requests", "backup_creations"):
                selectors.append((table, "request_id=?", (explicit_request_id,)))
        seen: set[tuple[str, str, tuple[Any, ...]]] = set()
        for selector in selectors:
            if selector in seen or selector[0] not in tables:
                continue
            seen.add(selector)
            snapshot["selected_tables"].append(selector[0])
            rows = _rows(conn, *selector)
            if rows:
                snapshot["tables"].setdefault(selector[0], []).extend(rows)
        for table, rows in snapshot["tables"].items():
            unique = {
                json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False): row
                for row in rows
            }
            snapshot["tables"][table] = [unique[key] for key in sorted(unique)]
        snapshot["selected_tables"] = sorted(set(snapshot["selected_tables"]))
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
    max_spool_bytes: int = 512 * 1024 * 1024
    max_spool_records: int = 100_000
    min_free_bytes: int = 1024 * 1024 * 1024

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
        if settings.mode in {"capture", "execute"}:
            require_distinct_paths({
                "authority database": self.db_path,
                "dark-launch spool": settings.spool_path,
                "dark-launch emergency directory": settings.emergency_dir,
                "dark-launch kill switch": settings.kill_switch_path,
            })
        self.spool = ShadowSpool(
            settings.spool_path,
            busy_timeout_ms=settings.busy_timeout_ms,
            max_bytes=settings.max_spool_bytes,
            max_records=settings.max_spool_records,
            min_free_bytes=settings.min_free_bytes,
        )
        self.emergency = EmergencyGapWriter(settings.emergency_dir)
        self._capture_available = settings.mode in {"capture", "execute"}
        if self._capture_available:
            try:
                self.spool.initialize()
            except BaseException:
                self._capture_available = False
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

    def _engage_capacity_kill_switch(self, error: ShadowSpoolCapacityError) -> None:
        path = self.settings.kill_switch_path
        if path is None:
            return
        engage_kill_switch(
            path,
            {
                "disabled": True,
                "reason": "dark-launch spool capacity guard",
                "error": str(error),
                "at": _utcnow().isoformat(),
            },
        )

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
        if not self.settings.enabled or not self._capture_available:
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
            if existing is None and self.spool.has_source_identity(identity):
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
                pinned_inputs={"capture_schema": 3, "rollout_mode": self.settings.mode},
                created_at=_utcnow(),
            )
        except ShadowSpoolCapacityError as exc:
            try:
                self._engage_capacity_kill_switch(exc)
            except BaseException:
                LOGGER.exception("dark-launch capacity kill-switch write failed")
            self._emergency(identity=identity, command=command, phase="capacity", error=exc)
            return call()
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
                result=result,
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
        except ShadowSpoolCapacityError as exc:
            try:
                self._engage_capacity_kill_switch(exc)
            except BaseException:
                LOGGER.exception("dark-launch completion capacity kill-switch write failed")
            try:
                self.spool.record_gap(
                    reservation.registration_id,
                    gap={
                        "classification": "capture_completion_capacity_exceeded",
                        "missing_evidence": ["source_post_state"],
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                    completed_at=_utcnow(),
                )
            except BaseException:
                self._emergency(identity=identity, command=command, phase="complete_capacity", error=exc)
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
