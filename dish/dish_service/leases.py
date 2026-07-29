"""Durable client/run leases layered over the operation task lock."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from dish_tool.errors import DishRuleError


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class ServicePrincipal:
    owner_id: str
    run_id: str

    @classmethod
    def from_values(cls, owner_id: str | None, run_id: str | None) -> "ServicePrincipal":
        owner = str(owner_id or "").strip()
        run = str(run_id or "").strip()
        if not owner:
            raise DishRuleError("INVALID_ARGUMENT", "service owner identity is required", rule="service_owner_required")
        if not run:
            raise DishRuleError("INVALID_ARGUMENT", "service run identity is required", rule="service_run_required")
        return cls(owner_id=owner, run_id=run)


class LeaseManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        ttl_seconds: int = 1800,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        self.conn = conn
        self.ttl_seconds = ttl_seconds
        self.now = now

    def active_for_operation(self, operation_id: str):
        return self.conn.execute(
            "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()

    def is_expired(self, row) -> bool:
        return _parse(row["expires_at"]) <= self.now()

    @staticmethod
    def is_owned_by(row, principal: ServicePrincipal) -> bool:
        return row["owner_id"] == principal.owner_id and row["run_id"] == principal.run_id

    def _operation(self, operation_id: str):
        row = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
        return row

    def _assert_owned_row(
        self,
        row,
        principal: ServicePrincipal,
        *,
        now: datetime,
    ):
        if row is None:
            raise DishRuleError(
                "CONFLICT", "operation has no active service lease",
                rule="service_lease_missing",
            )
        if _parse(row["expires_at"]) <= now:
            raise DishRuleError(
                "CONFLICT",
                "service lease expired and requires administrative recovery",
                rule="service_lease_expired",
                details={"expires_at": row["expires_at"]},
            )
        if not self.is_owned_by(row, principal):
            raise DishRuleError(
                "AGENT_MISMATCH", "service lease belongs to another client run",
                rule="service_lease_owner_mismatch",
                details={"owner_id": row["owner_id"], "run_id": row["run_id"]},
            )
        return row

    def _release_row(self, row, *, reason: str, now: datetime):
        cursor = self.conn.execute(
            """UPDATE service_leases
                  SET released_at=?, release_reason=?
                WHERE lease_id=? AND released_at IS NULL""",
            (_stamp(now), str(reason).strip() or "released", row["lease_id"]),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT", "service lease changed before release",
                rule="service_lease_conflict",
            )
        return self.conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (row["lease_id"],)
        ).fetchone()

    def _reap_terminal_task_lease(self, row, *, now: datetime) -> bool:
        """Release a stale terminal lease only when local completion is safe.

        Workflow terminal status already removes every legal mutation.  The
        lease can linger if a process stops between workflow commit and service
        cleanup, but it must not block a later operation for the same task.
        Pending workflow steps or unresolved external attempts remain recovery
        evidence and deliberately prevent automatic reaping.
        """
        operation = self._operation(row["operation_id"])
        if (
            operation["status"] not in {"completed", "cancelled"}
            or operation["phase"] != "terminal"
            or not operation["completed_at"]
            or not operation["terminal_outcome"]
        ):
            return False
        pending = self.conn.execute(
            "SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NULL LIMIT 1",
            (row["operation_id"],),
        ).fetchone()
        unresolved = self.conn.execute(
            """SELECT 1 FROM write_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               UNION ALL
               SELECT 1 FROM movement_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               LIMIT 1""",
            (row["operation_id"], row["operation_id"]),
        ).fetchone()
        if pending is not None or unresolved is not None:
            return False
        self._release_row(row, reason="terminal_lease_reaped", now=now)
        return True

    def acquire(self, operation_id: str, principal: ServicePrincipal):
        now = self.now()
        expiry = now + timedelta(seconds=self.ttl_seconds)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            op = self._operation(operation_id)
            if op["status"] != "open":
                raise DishRuleError(
                    "WRONG_STATE",
                    "service lease can be acquired only for an open operation",
                    rule="service_lease_operation_not_open",
                    details={"operation_id": operation_id, "status": op["status"]},
                )
            existing = self.active_for_operation(operation_id)
            if existing is not None:
                if self.is_owned_by(existing, principal):
                    if _parse(existing["expires_at"]) <= now:
                        raise DishRuleError(
                            "CONFLICT",
                            "service lease expired and requires administrative recovery",
                            rule="service_lease_expired",
                            details={"operation_id": operation_id, "expires_at": existing["expires_at"]},
                        )
                    self.conn.execute("COMMIT")
                    return existing
                expired = _parse(existing["expires_at"]) <= now
                raise DishRuleError(
                    "CONFLICT",
                    "service lease expired and requires administrative recovery"
                    if expired else "operation is leased to another client run",
                    rule="service_lease_expired" if expired else "service_lease_held",
                    details={
                        "operation_id": operation_id,
                        "owner_id": existing["owner_id"],
                        "run_id": existing["run_id"],
                        "expires_at": existing["expires_at"],
                    },
                )
            task_existing = self.conn.execute(
                "SELECT * FROM service_leases WHERE task_gid=? AND released_at IS NULL",
                (op["task_gid"],),
            ).fetchone()
            if task_existing is not None and self._reap_terminal_task_lease(
                task_existing, now=now
            ):
                task_existing = None
            if task_existing is not None:
                raise DishRuleError(
                    "CONFLICT", "task is leased by another active operation",
                    rule="task_lease_held",
                    details={"operation_id": task_existing["operation_id"]},
                )
            lease_id = str(uuid.uuid4())
            stamp = _stamp(now)
            self.conn.execute(
                """INSERT INTO service_leases(
                       lease_id,operation_id,task_gid,owner_id,run_id,
                       acquired_at,renewed_at,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (lease_id, operation_id, op["task_gid"], principal.owner_id,
                 principal.run_id, stamp, stamp, _stamp(expiry)),
            )
            row = self.conn.execute(
                "SELECT * FROM service_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
            self.conn.execute("COMMIT")
            return row
        except sqlite3.IntegrityError as exc:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise DishRuleError(
                "CONFLICT", "service lease acquisition collided",
                rule="service_lease_conflict",
            ) from exc
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def assert_owned(self, operation_id: str, principal: ServicePrincipal):
        return self._assert_owned_row(
            self.active_for_operation(operation_id), principal, now=self.now()
        )

    def renew(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        manage_transaction: bool = True,
    ):
        now = self.now()
        expiry = now + timedelta(seconds=self.ttl_seconds)
        if manage_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        elif not self.conn.in_transaction:
            raise RuntimeError("lease renewal requires an active caller transaction")
        try:
            op = self._operation(operation_id)
            if op["status"] != "open":
                raise DishRuleError(
                    "WRONG_STATE",
                    "service lease cannot be renewed for a terminal operation",
                    rule="service_lease_operation_not_open",
                    details={"operation_id": operation_id, "status": op["status"]},
                )
            row = self._assert_owned_row(
                self.active_for_operation(operation_id), principal, now=now
            )
            cursor = self.conn.execute(
                """UPDATE service_leases SET renewed_at=?, expires_at=?
                     WHERE lease_id=? AND released_at IS NULL
                       AND owner_id=? AND run_id=? AND expires_at>?""",
                (_stamp(now), _stamp(expiry), row["lease_id"], principal.owner_id,
                 principal.run_id, _stamp(now)),
            )
            if cursor.rowcount != 1:
                raise DishRuleError(
                    "CONFLICT", "service lease changed before renewal",
                    rule="service_lease_conflict",
                )
            renewed = self.active_for_operation(operation_id)
            if manage_transaction:
                self.conn.execute("COMMIT")
            return renewed
        except Exception:
            if manage_transaction and self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def release(
        self,
        operation_id: str,
        principal: ServicePrincipal | None,
        *,
        reason: str,
        admin: bool = False,
    ):
        now = self.now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.active_for_operation(operation_id)
            if row is None:
                self.conn.execute("COMMIT")
                return None
            if not admin:
                if principal is None:
                    raise DishRuleError(
                        "INVALID_ARGUMENT", "service principal is required",
                        rule="service_principal_required",
                    )
                self._assert_owned_row(row, principal, now=now)
            released = self._release_row(row, reason=reason, now=now)
            self.conn.execute("COMMIT")
            return released
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def release_for_handoff(
        self, operation_id: str, principal: ServicePrincipal, *, reason: str
    ):
        now = self.now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            op = self._operation(operation_id)
            row = self._assert_owned_row(
                self.active_for_operation(operation_id), principal, now=now
            )
            if op["status"] in {"open", "uncertain"} and op["phase"] not in {
                "await_verification", "held_evidence", "held_human"
            }:
                raise DishRuleError(
                    "WRONG_STATE",
                    "owner lease cannot be released before a workflow handoff",
                    rule="service_lease_release_forbidden",
                    details={"phase": op["phase"]},
                )
            unresolved = self.conn.execute(
                """SELECT 1 FROM write_attempts WHERE operation_id=? AND outcome IN ('started','uncertain')
                   UNION ALL
                   SELECT 1 FROM movement_attempts WHERE operation_id=? AND outcome IN ('started','uncertain')
                   LIMIT 1""",
                (operation_id, operation_id),
            ).fetchone()
            pending = self.conn.execute(
                "SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NULL LIMIT 1",
                (operation_id,),
            ).fetchone()
            if unresolved or pending:
                raise DishRuleError(
                    "WRONG_STATE",
                    "owner lease cannot be released before durable completion markers",
                    rule="service_lease_completion_pending",
                )
            released = self._release_row(row, reason=reason, now=now)
            self.conn.execute("COMMIT")
            return released
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def release_terminal(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str = "operation_terminal",
    ):
        now = self.now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            op = self._operation(operation_id)
            row = self.active_for_operation(operation_id)
            if row is None and op["status"] in {"completed", "cancelled"}:
                # A later operation may have safely reaped this cleanup tail
                # after terminal workflow commit but before this request's own
                # response bookkeeping reached lease release.
                self.conn.execute("COMMIT")
                return None
            row = self._assert_owned_row(row, principal, now=now)
            if op["status"] not in {"completed", "cancelled"}:
                raise DishRuleError(
                    "WRONG_STATE",
                    "task lock remains active until the operation is terminal",
                    rule="service_task_lock_active",
                )
            pending = self.conn.execute(
                "SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NULL LIMIT 1",
                (operation_id,),
            ).fetchone()
            unresolved = self.conn.execute(
                """SELECT 1 FROM write_attempts WHERE operation_id=? AND outcome IN ('started','uncertain')
                   UNION ALL
                   SELECT 1 FROM movement_attempts WHERE operation_id=? AND outcome IN ('started','uncertain')
                   LIMIT 1""",
                (operation_id, operation_id),
            ).fetchone()
            if pending or unresolved:
                raise DishRuleError(
                    "WRONG_STATE",
                    "task lock cannot release before all completion markers are durable",
                    rule="service_lease_completion_pending",
                )
            released = self._release_row(row, reason=reason, now=now)
            self.conn.execute("COMMIT")
            return released
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def admin_recover(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
        manage_transaction: bool = True,
    ):
        del principal
        now = self.now()
        if manage_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        elif not self.conn.in_transaction:
            raise RuntimeError("lease recovery requires an active caller transaction")
        try:
            self._operation(operation_id)
            row = self.active_for_operation(operation_id)
            if row is None:
                self.conn.execute("COMMIT")
                return None
            if _parse(row["expires_at"]) > now:
                raise DishRuleError(
                    "CONFLICT", "active lease is not stale",
                    rule="service_lease_not_stale",
                    details={"expires_at": row["expires_at"]},
                )
            released = self._release_row(
                row, reason=f"admin recovery: {reason}", now=now
            )
            if manage_transaction:
                self.conn.execute("COMMIT")
            return released
        except Exception:
            if manage_transaction and self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
