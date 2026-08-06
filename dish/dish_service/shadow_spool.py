"""Durable local spool for Asana-authoritative dark-launch capture.

The spool is intentionally independent of PostgreSQL. A live command reserves a
monotonic rollout sequence before the authoritative command runs, then records
one complete envelope or one explicit gap. PostgreSQL delivery is asynchronous;
spool failure is never reported as command success evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from dish_tool.transactions import immediate_transaction


class ShadowSpoolError(RuntimeError):
    pass


class ShadowSpoolConflict(ShadowSpoolError):
    pass


class ShadowSpoolCapacityError(ShadowSpoolError):
    pass


def _canonical_json(value: Mapping[str, Any] | list[Any] | None) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Mapping[str, Any] | list[Any] | None) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dark-launch timestamps must include an offset")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class ShadowReservation:
    registration_id: str
    source_request_identity: str
    rollout_sequence: int
    command_name: str
    treatment: str
    state: str


@dataclass(frozen=True)
class ShadowSpoolItem:
    registration_id: str
    source_request_identity: str
    rollout_sequence: int
    source_authority_generation: str
    command_name: str
    treatment: str
    canonical_input: Mapping[str, Any]
    principal: Mapping[str, Any]
    source_pre_state: Mapping[str, Any]
    pinned_inputs: Mapping[str, Any]
    source_outcome: Mapping[str, Any] | None
    source_post_state: Mapping[str, Any] | None
    source_effects: Mapping[str, Any] | None
    gap: Mapping[str, Any] | None
    state: str
    created_at: datetime
    completed_at: datetime | None
    delivery_attempts: int
    last_delivery_error: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_spool_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO shadow_spool_metadata(key, value) VALUES ('next_rollout_sequence', '1');

CREATE TABLE IF NOT EXISTS shadow_capture_registrations (
    registration_id TEXT PRIMARY KEY,
    source_request_identity TEXT NOT NULL UNIQUE,
    rollout_sequence INTEGER NOT NULL UNIQUE CHECK (rollout_sequence > 0),
    source_authority_generation TEXT NOT NULL,
    command_name TEXT NOT NULL,
    treatment TEXT NOT NULL CHECK (treatment IN ('execute','capture_only','excluded')),
    canonical_input_json TEXT NOT NULL CHECK (json_valid(canonical_input_json)),
    canonical_input_sha256 TEXT NOT NULL CHECK (length(canonical_input_sha256)=64),
    principal_json TEXT NOT NULL CHECK (json_valid(principal_json)),
    principal_sha256 TEXT NOT NULL CHECK (length(principal_sha256)=64),
    source_pre_state_json TEXT NOT NULL CHECK (json_valid(source_pre_state_json)),
    source_pre_state_sha256 TEXT NOT NULL CHECK (length(source_pre_state_sha256)=64),
    pinned_inputs_json TEXT NOT NULL CHECK (json_valid(pinned_inputs_json)),
    pinned_inputs_sha256 TEXT NOT NULL CHECK (length(pinned_inputs_sha256)=64),
    state TEXT NOT NULL CHECK (state IN ('reserved','complete','gap','delivered')),
    source_outcome_json TEXT CHECK (source_outcome_json IS NULL OR json_valid(source_outcome_json)),
    source_outcome_sha256 TEXT CHECK (source_outcome_sha256 IS NULL OR length(source_outcome_sha256)=64),
    source_post_state_json TEXT CHECK (source_post_state_json IS NULL OR json_valid(source_post_state_json)),
    source_post_state_sha256 TEXT CHECK (source_post_state_sha256 IS NULL OR length(source_post_state_sha256)=64),
    source_effects_json TEXT CHECK (source_effects_json IS NULL OR json_valid(source_effects_json)),
    gap_json TEXT CHECK (gap_json IS NULL OR json_valid(gap_json)),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    delivered_at TEXT,
    delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    last_delivery_error TEXT,
    CHECK (
        (state='reserved' AND source_outcome_json IS NULL AND source_post_state_json IS NULL AND gap_json IS NULL AND completed_at IS NULL AND delivered_at IS NULL)
        OR (state='complete' AND source_outcome_json IS NOT NULL AND source_post_state_json IS NOT NULL AND gap_json IS NULL AND completed_at IS NOT NULL AND delivered_at IS NULL)
        OR (state='gap' AND gap_json IS NOT NULL AND completed_at IS NOT NULL AND delivered_at IS NULL)
        OR (state='delivered' AND completed_at IS NOT NULL AND delivered_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS shadow_capture_pending_idx
    ON shadow_capture_registrations(state, rollout_sequence);

CREATE TABLE IF NOT EXISTS shadow_capture_tombstones (
    registration_id TEXT PRIMARY KEY,
    source_request_identity TEXT NOT NULL UNIQUE,
    rollout_sequence INTEGER NOT NULL UNIQUE CHECK (rollout_sequence > 0),
    source_authority_generation TEXT NOT NULL,
    command_name TEXT NOT NULL,
    treatment TEXT NOT NULL CHECK (treatment IN ('execute','capture_only','excluded')),
    canonical_input_sha256 TEXT NOT NULL CHECK (length(canonical_input_sha256)=64),
    principal_sha256 TEXT NOT NULL CHECK (length(principal_sha256)=64),
    source_pre_state_sha256 TEXT NOT NULL CHECK (length(source_pre_state_sha256)=64),
    pinned_inputs_sha256 TEXT NOT NULL CHECK (length(pinned_inputs_sha256)=64),
    delivered_at TEXT NOT NULL,
    compacted_at TEXT NOT NULL
);
"""


class ShadowSpool:
    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = 50,
        max_bytes: int = 512 * 1024 * 1024,
        max_records: int = 100_000,
        min_free_bytes: int = 1024 * 1024 * 1024,
        create_if_missing: bool = True,
        read_only: bool = False,
        immutable_read_only: bool = False,
    ) -> None:
        for name, value in (
            ("busy_timeout_ms", busy_timeout_ms),
            ("max_bytes", max_bytes),
            ("max_records", max_records),
            ("min_free_bytes", min_free_bytes),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.path = Path(path).expanduser()
        self.busy_timeout_ms = busy_timeout_ms
        self.max_bytes = max_bytes
        self.max_records = max_records
        self.min_free_bytes = min_free_bytes
        self.create_if_missing = bool(create_if_missing)
        self.read_only = bool(read_only)
        self.immutable_read_only = bool(immutable_read_only)
        if self.immutable_read_only and not self.read_only:
            raise ValueError("immutable_read_only requires read_only")
        if self.create_if_missing and self.read_only:
            raise ValueError("read_only spool cannot create a database")
        self._initialize_lock = threading.Lock()
        self._initialized = False

    @classmethod
    def open_existing(cls, path: Path, **kwargs: Any) -> "ShadowSpool":
        """Open an existing spool without ever creating a replacement database."""
        return cls(path, create_if_missing=False, **kwargs)

    @classmethod
    def open_existing_read_only(cls, path: Path, **kwargs: Any) -> "ShadowSpool":
        """Inspect a quiescent existing spool without creating SQLite sidecars.

        SQLite read-only WAL access can create or update ``-shm`` state.  The
        strict inspection path therefore fails closed when a journal sidecar is
        present and uses SQLite's immutable mode for a checkpointed database.
        """
        target = Path(path).expanduser()
        for suffix in ("-journal", "-wal"):
            if Path(f"{target}{suffix}").exists():
                raise ShadowSpoolError(
                    "read-only spool inspection requires a quiescent checkpointed database"
                )
        return cls(
            target,
            create_if_missing=False,
            read_only=True,
            immutable_read_only=True,
            **kwargs,
        )

    @classmethod
    def open_existing_live_read_only(cls, path: Path, **kwargs: Any) -> "ShadowSpool":
        """Observe an active spool without writing rows or schema.

        SQLite may update its WAL shared-memory coordination file while this
        observer is connected.  Use :meth:`open_existing_read_only` for the
        stricter preflight contract that must not touch source sidecars.
        """
        return cls(path, create_if_missing=False, read_only=True, **kwargs)

    def _open(self) -> sqlite3.Connection:
        if self.create_if_missing:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target, uri = str(self.path), False
        else:
            if not self.path.is_file():
                raise ShadowSpoolError(f"dark-launch spool does not exist: {self.path}")
            if self.read_only:
                immutable = "&immutable=1" if self.immutable_read_only else ""
                target = f"{self.path.resolve().as_uri()}?mode=ro{immutable}"
            else:
                target = f"{self.path.resolve().as_uri()}?mode=rw"
            uri = True
        conn = sqlite3.connect(
            target, uri=uri, timeout=self.busy_timeout_ms / 1000, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        if not self.read_only:
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def initialize(self) -> None:
        """Create/upgrade the non-authoritative spool outside the live request path."""
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            conn = self._open()
            try:
                if self.create_if_missing:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(_SCHEMA)
                else:
                    required = {
                        "shadow_spool_metadata",
                        "shadow_capture_registrations",
                        "shadow_capture_tombstones",
                    }
                    present = {
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_schema WHERE type='table'"
                        )
                    }
                    if not required.issubset(present):
                        raise ShadowSpoolError("existing dark-launch spool schema is incomplete")
                    marker = conn.execute(
                        "SELECT value FROM shadow_spool_metadata WHERE key='next_rollout_sequence'"
                    ).fetchone()
                    if marker is None:
                        raise ShadowSpoolError("existing dark-launch spool metadata is incomplete")
            finally:
                conn.close()
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return self._open()

    def _capacity_state(self, conn: sqlite3.Connection) -> dict[str, int]:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        sidecar_bytes = sum(
            candidate.stat().st_size if candidate.exists() else 0
            for candidate in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm"))
        )
        active_records = int(
            conn.execute("SELECT COUNT(*) FROM shadow_capture_registrations").fetchone()[0]
        )
        archived_records = int(
            conn.execute("SELECT COUNT(*) FROM shadow_capture_tombstones").fetchone()[0]
        )
        return {
            "logical_bytes": max(0, page_count - free_pages) * page_size + sidecar_bytes,
            "physical_bytes": sum(
                candidate.stat().st_size if candidate.exists() else 0
                for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm"))
            ),
            "free_bytes": shutil.disk_usage(self.path.parent).free,
            "active_records": active_records,
            "archived_records": archived_records,
            "total_records": active_records + archived_records,
        }

    def _assert_capacity(
        self,
        conn: sqlite3.Connection,
        *,
        incoming_bytes: int,
        additional_records: int = 0,
    ) -> None:
        state = self._capacity_state(conn)
        if state["total_records"] + additional_records > self.max_records:
            raise ShadowSpoolCapacityError("dark-launch spool record limit reached")
        if state["logical_bytes"] + incoming_bytes > self.max_bytes:
            raise ShadowSpoolCapacityError("dark-launch spool byte limit reached")
        if state["free_bytes"] - incoming_bytes < self.min_free_bytes:
            raise ShadowSpoolCapacityError("dark-launch spool free-space floor reached")

    @staticmethod
    def _identity_fingerprint(values: tuple[str, ...]) -> tuple[str, ...]:
        return (values[0], values[1], values[2], values[4], values[6], values[8], values[10])

    @staticmethod
    def _identity_payload(
        *,
        source_authority_generation: str,
        command_name: str,
        treatment: str,
        canonical_input: Mapping[str, Any],
        principal: Mapping[str, Any],
        source_pre_state: Mapping[str, Any],
        pinned_inputs: Mapping[str, Any],
    ) -> tuple[str, ...]:
        return (
            source_authority_generation,
            command_name,
            treatment,
            _canonical_json(canonical_input),
            _sha256_json(canonical_input),
            _canonical_json(principal),
            _sha256_json(principal),
            _canonical_json(source_pre_state),
            _sha256_json(source_pre_state),
            _canonical_json(pinned_inputs),
            _sha256_json(pinned_inputs),
        )

    def reserve(
        self,
        *,
        source_request_identity: str,
        source_authority_generation: str,
        command_name: str,
        treatment: str,
        canonical_input: Mapping[str, Any],
        principal: Mapping[str, Any],
        source_pre_state: Mapping[str, Any],
        pinned_inputs: Mapping[str, Any],
        created_at: datetime,
        registration_id: str | None = None,
    ) -> ShadowReservation:
        identity = str(source_request_identity).strip()
        if not identity:
            raise ValueError("source_request_identity is required")
        if treatment not in {"execute", "capture_only", "excluded"}:
            raise ValueError("invalid dark-launch treatment")
        values = self._identity_payload(
            source_authority_generation=source_authority_generation,
            command_name=command_name,
            treatment=treatment,
            canonical_input=canonical_input,
            principal=principal,
            source_pre_state=source_pre_state,
            pinned_inputs=pinned_inputs,
        )
        conn = self._connect()
        try:
            with immediate_transaction(conn, "shadow_spool_reserve"):
                existing = conn.execute(
                    "SELECT * FROM shadow_capture_registrations WHERE source_request_identity=?",
                    (identity,),
                ).fetchone()
                if existing is not None:
                    observed = (
                        existing["source_authority_generation"],
                        existing["command_name"],
                        existing["treatment"],
                        existing["canonical_input_json"],
                        existing["canonical_input_sha256"],
                        existing["principal_json"],
                        existing["principal_sha256"],
                        existing["source_pre_state_json"],
                        existing["source_pre_state_sha256"],
                        existing["pinned_inputs_json"],
                        existing["pinned_inputs_sha256"],
                    )
                    if observed != values:
                        raise ShadowSpoolConflict(
                            "source request identity conflicts with existing reservation"
                        )
                    return ShadowReservation(
                        existing["registration_id"], identity,
                        int(existing["rollout_sequence"]), existing["command_name"],
                        existing["treatment"], existing["state"],
                    )
                archived = conn.execute(
                    "SELECT * FROM shadow_capture_tombstones WHERE source_request_identity=?",
                    (identity,),
                ).fetchone()
                if archived is not None:
                    observed = (
                        archived["source_authority_generation"],
                        archived["command_name"],
                        archived["treatment"],
                        archived["canonical_input_sha256"],
                        archived["principal_sha256"],
                        archived["source_pre_state_sha256"],
                        archived["pinned_inputs_sha256"],
                    )
                    if observed != self._identity_fingerprint(values):
                        raise ShadowSpoolConflict(
                            "source request identity conflicts with compacted evidence"
                        )
                    return ShadowReservation(
                        archived["registration_id"], identity,
                        int(archived["rollout_sequence"]), archived["command_name"],
                        archived["treatment"], "delivered",
                    )
                incoming_bytes = sum(
                    len(value.encode("utf-8")) for value in values
                ) + 2048
                self._assert_capacity(
                    conn, incoming_bytes=incoming_bytes, additional_records=1
                )
                next_row = conn.execute(
                    "SELECT value FROM shadow_spool_metadata WHERE key='next_rollout_sequence'"
                ).fetchone()
                sequence = int(next_row["value"])
                conn.execute(
                    "UPDATE shadow_spool_metadata SET value=? WHERE key='next_rollout_sequence'",
                    (str(sequence + 1),),
                )
                row_id = registration_id or str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO shadow_capture_registrations(
                           registration_id, source_request_identity, rollout_sequence,
                           source_authority_generation, command_name, treatment,
                           canonical_input_json, canonical_input_sha256,
                           principal_json, principal_sha256,
                           source_pre_state_json, source_pre_state_sha256,
                           pinned_inputs_json, pinned_inputs_sha256,
                           state, created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'reserved', ?)""",
                    (row_id, identity, sequence, *values, _stamp(created_at)),
                )
                return ShadowReservation(
                    row_id, identity, sequence, command_name, treatment, "reserved"
                )
        finally:
            conn.close()

    def complete(
        self,
        registration_id: str,
        *,
        source_outcome: Mapping[str, Any],
        source_post_state: Mapping[str, Any],
        source_effects: Mapping[str, Any],
        completed_at: datetime,
    ) -> ShadowReservation:
        outcome_json = _canonical_json(source_outcome)
        post_json = _canonical_json(source_post_state)
        effects_json = _canonical_json(source_effects)
        conn = self._connect()
        try:
            with immediate_transaction(conn, "shadow_spool_complete"):
                row = conn.execute(
                    "SELECT * FROM shadow_capture_registrations WHERE registration_id=?",
                    (registration_id,),
                ).fetchone()
                if row is None:
                    raise ShadowSpoolError("shadow reservation does not exist")
                if row["state"] in {"complete", "delivered"}:
                    if (
                        row["source_outcome_sha256"] != _sha256_json(source_outcome)
                        or row["source_post_state_sha256"] != _sha256_json(source_post_state)
                        or row["source_effects_json"] != effects_json
                    ):
                        raise ShadowSpoolConflict(
                            "completed shadow reservation conflicts with replay"
                        )
                elif row["state"] != "reserved":
                    raise ShadowSpoolConflict("shadow reservation is already a gap")
                else:
                    incoming_bytes = sum(
                        len(value.encode("utf-8"))
                        for value in (outcome_json, post_json, effects_json)
                    ) + 2048
                    self._assert_capacity(
                        conn, incoming_bytes=incoming_bytes, additional_records=0
                    )
                    conn.execute(
                        """UPDATE shadow_capture_registrations
                              SET state='complete', source_outcome_json=?, source_outcome_sha256=?,
                                  source_post_state_json=?, source_post_state_sha256=?,
                                  source_effects_json=?, completed_at=?
                            WHERE registration_id=? AND state='reserved'""",
                        (
                            outcome_json, _sha256_json(source_outcome), post_json,
                            _sha256_json(source_post_state), effects_json,
                            _stamp(completed_at), registration_id,
                        ),
                    )
                    self._assert_capacity(conn, incoming_bytes=0, additional_records=0)
                return ShadowReservation(
                    row["registration_id"], row["source_request_identity"],
                    int(row["rollout_sequence"]), row["command_name"], row["treatment"],
                    "complete" if row["state"] != "delivered" else "delivered",
                )
        finally:
            conn.close()

    def record_gap(
        self,
        registration_id: str,
        *,
        gap: Mapping[str, Any],
        completed_at: datetime,
    ) -> ShadowReservation:
        gap_json = _canonical_json(gap)
        conn = self._connect()
        try:
            with immediate_transaction(conn, "shadow_spool_record_gap"):
                row = conn.execute(
                    "SELECT * FROM shadow_capture_registrations WHERE registration_id=?",
                    (registration_id,),
                ).fetchone()
                if row is None:
                    raise ShadowSpoolError("shadow reservation does not exist")
                if row["state"] == "gap":
                    if row["gap_json"] != gap_json:
                        raise ShadowSpoolConflict(
                            "shadow gap conflicts with existing evidence"
                        )
                elif row["state"] != "reserved":
                    raise ShadowSpoolConflict(
                        "completed shadow reservation cannot become a gap"
                    )
                else:
                    self._assert_capacity(
                        conn,
                        incoming_bytes=len(gap_json.encode("utf-8")) + 512,
                        additional_records=0,
                    )
                    conn.execute(
                        """UPDATE shadow_capture_registrations
                              SET state='gap', gap_json=?, completed_at=?
                            WHERE registration_id=? AND state='reserved'""",
                        (gap_json, _stamp(completed_at), registration_id),
                    )
                    self._assert_capacity(conn, incoming_bytes=0, additional_records=0)
                return ShadowReservation(
                    row["registration_id"], row["source_request_identity"],
                    int(row["rollout_sequence"]), row["command_name"], row["treatment"],
                    "gap",
                )
        finally:
            conn.close()

    def recover_stale_reservations(
        self,
        *,
        now: datetime,
        older_than: timedelta,
    ) -> int:
        cutoff = _stamp(now - older_than)
        gap = _canonical_json({
            "failure_stage": "command_completion_capture",
            "missing_evidence": ["source_outcome", "source_post_state"],
            "repair": "retain as permanent command-level proof gap",
        })
        conn = self._connect()
        try:
            candidate = conn.execute(
                """SELECT 1 FROM shadow_capture_registrations
                    WHERE state='reserved' AND created_at <= ? LIMIT 1""",
                (cutoff,),
            ).fetchone()
            if candidate is None:
                return 0
            with immediate_transaction(conn, "shadow_spool_recover_stale"):
                result = conn.execute(
                    """UPDATE shadow_capture_registrations
                          SET state='gap', gap_json=?, completed_at=?
                        WHERE state='reserved' AND created_at <= ?""",
                    (gap, _stamp(now), cutoff),
                )
                return int(result.rowcount)
        finally:
            conn.close()

    def get_by_source_identity(self, source_request_identity: str) -> ShadowSpoolItem | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM shadow_capture_registrations WHERE source_request_identity=?",
                (source_request_identity,),
            ).fetchone()
            return None if row is None else self._item(row)
        finally:
            conn.close()

    def has_source_identity(self, source_request_identity: str) -> bool:
        conn = self._connect()
        try:
            return conn.execute(
                """SELECT 1 FROM shadow_capture_registrations WHERE source_request_identity=?
                   UNION ALL
                   SELECT 1 FROM shadow_capture_tombstones WHERE source_request_identity=?
                   LIMIT 1""",
                (source_request_identity, source_request_identity),
            ).fetchone() is not None
        finally:
            conn.close()

    def compact_delivered(
        self,
        *,
        now: datetime,
        older_than: timedelta,
        limit: int = 1000,
    ) -> int:
        if older_than.total_seconds() < 0:
            raise ValueError("older_than must not be negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        cutoff = _stamp(now - older_than)
        conn = self._connect()
        try:
            candidate_ids = [
                row["registration_id"]
                for row in conn.execute(
                    """SELECT registration_id FROM shadow_capture_registrations
                        WHERE state='delivered' AND delivered_at <= ?
                        ORDER BY rollout_sequence LIMIT ?""",
                    (cutoff, limit),
                ).fetchall()
            ]
            if not candidate_ids:
                return 0
            with immediate_transaction(conn, "shadow_spool_compact_delivered"):
                placeholders = ",".join("?" for _ in candidate_ids)
                rows = conn.execute(
                    f"""SELECT * FROM shadow_capture_registrations
                         WHERE state='delivered' AND delivered_at <= ?
                           AND registration_id IN ({placeholders})
                         ORDER BY rollout_sequence""",
                    (cutoff, *candidate_ids),
                ).fetchall()
                for row in rows:
                    conn.execute(
                        """INSERT INTO shadow_capture_tombstones(
                               registration_id, source_request_identity, rollout_sequence,
                               source_authority_generation, command_name, treatment,
                               canonical_input_sha256, principal_sha256,
                               source_pre_state_sha256, pinned_inputs_sha256,
                               delivered_at, compacted_at
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            row["registration_id"], row["source_request_identity"],
                            row["rollout_sequence"], row["source_authority_generation"],
                            row["command_name"], row["treatment"],
                            row["canonical_input_sha256"], row["principal_sha256"],
                            row["source_pre_state_sha256"], row["pinned_inputs_sha256"],
                            row["delivered_at"], _stamp(now),
                        ),
                    )
                    conn.execute(
                        "DELETE FROM shadow_capture_registrations WHERE registration_id=? AND state='delivered'",
                        (row["registration_id"],),
                    )
                return len(rows)
        finally:
            conn.close()

    def pending(self, *, limit: int = 100) -> tuple[ShadowSpoolItem, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM shadow_capture_registrations
                    WHERE state IN ('complete','gap')
                      AND rollout_sequence < COALESCE(
                          (SELECT MIN(rollout_sequence)
                             FROM shadow_capture_registrations
                            WHERE state='reserved'),
                          9223372036854775807
                      )
                    ORDER BY rollout_sequence LIMIT ?""",
                (limit,),
            ).fetchall()
            return tuple(self._item(row) for row in rows)
        finally:
            conn.close()

    def mark_delivery_failed(self, registration_id: str, *, error: str) -> None:
        conn = self._connect()
        try:
            with immediate_transaction(conn, "shadow_spool_delivery_failed"):
                result = conn.execute(
                    """UPDATE shadow_capture_registrations
                          SET delivery_attempts=delivery_attempts+1, last_delivery_error=?
                        WHERE registration_id=? AND state IN ('complete','gap')""",
                    (str(error), registration_id),
                )
                if result.rowcount != 1:
                    raise ShadowSpoolConflict("shadow item is not pending delivery")
        finally:
            conn.close()

    def mark_delivered(self, registration_id: str, *, delivered_at: datetime) -> None:
        conn = self._connect()
        try:
            with immediate_transaction(conn, "shadow_spool_mark_delivered"):
                result = conn.execute(
                    """UPDATE shadow_capture_registrations
                          SET state='delivered', delivered_at=?, delivery_attempts=delivery_attempts+1,
                              last_delivery_error=NULL
                        WHERE registration_id=? AND state IN ('complete','gap')""",
                    (_stamp(delivered_at), registration_id),
                )
                if result.rowcount != 1:
                    existing = conn.execute(
                        "SELECT state FROM shadow_capture_registrations WHERE registration_id=?",
                        (registration_id,),
                    ).fetchone()
                    if existing is None or existing["state"] != "delivered":
                        raise ShadowSpoolConflict("shadow item is not pending delivery")
        finally:
            conn.close()

    def status(self) -> Mapping[str, Any]:
        conn = self._connect()
        try:
            counts = {
                row["state"]: int(row["count"])
                for row in conn.execute(
                    "SELECT state, COUNT(*) AS count FROM shadow_capture_registrations GROUP BY state"
                )
            }
            oldest = conn.execute(
                """SELECT rollout_sequence, created_at FROM shadow_capture_registrations
                    WHERE state IN ('reserved','complete','gap')
                    ORDER BY rollout_sequence LIMIT 1"""
            ).fetchone()
            next_sequence = int(conn.execute(
                "SELECT value FROM shadow_spool_metadata WHERE key='next_rollout_sequence'"
            ).fetchone()["value"])
            capacity = self._capacity_state(conn)
            return {
                "path": str(self.path),
                "counts": {
                    **{key: counts.get(key, 0) for key in ("reserved", "complete", "gap", "delivered")},
                    "archived": capacity["archived_records"],
                },
                "capacity": {
                    **capacity,
                    "max_bytes": self.max_bytes,
                    "max_records": self.max_records,
                    "min_free_bytes": self.min_free_bytes,
                    "accepting_new_records": (
                        capacity["total_records"] < self.max_records
                        and capacity["logical_bytes"] < self.max_bytes
                        and capacity["free_bytes"] >= self.min_free_bytes
                    ),
                },
                "next_rollout_sequence": next_sequence,
                "oldest_pending_sequence": None if oldest is None else int(oldest["rollout_sequence"]),
                "oldest_pending_at": None if oldest is None else oldest["created_at"],
            }
        finally:
            conn.close()

    @staticmethod
    def _item(row: sqlite3.Row) -> ShadowSpoolItem:
        load = lambda name: None if row[name] is None else json.loads(row[name])
        return ShadowSpoolItem(
            registration_id=row["registration_id"],
            source_request_identity=row["source_request_identity"],
            rollout_sequence=int(row["rollout_sequence"]),
            source_authority_generation=row["source_authority_generation"],
            command_name=row["command_name"],
            treatment=row["treatment"],
            canonical_input=load("canonical_input_json"),
            principal=load("principal_json"),
            source_pre_state=load("source_pre_state_json"),
            pinned_inputs=load("pinned_inputs_json"),
            source_outcome=load("source_outcome_json"),
            source_post_state=load("source_post_state_json"),
            source_effects=load("source_effects_json"),
            gap=load("gap_json"),
            state=row["state"],
            created_at=_parse(row["created_at"]),
            completed_at=None if row["completed_at"] is None else _parse(row["completed_at"]),
            delivery_attempts=int(row["delivery_attempts"]),
            last_delivery_error=row["last_delivery_error"],
        )


class EmergencyGapWriter:
    """Atomic filesystem fallback when the SQLite spool cannot be opened."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory).expanduser()

    def write(self, *, identity: str, payload: Mapping[str, Any]) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{identity}.json"
        body = (_canonical_json(payload) + "\n").encode("utf-8")
        fd, temp_name = tempfile.mkstemp(prefix=f".{identity}.", dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return target
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
