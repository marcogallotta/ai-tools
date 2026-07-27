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

    def _operation(self, operation_id: str):
        row = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
        return row

    def acquire(self, operation_id: str, principal: ServicePrincipal):
        op = self._operation(operation_id)
        now = self.now()
        expiry = now + timedelta(seconds=self.ttl_seconds)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if existing["owner_id"] == principal.owner_id and existing["run_id"] == principal.run_id:
                    if _parse(existing["expires_at"]) <= now:
                        raise DishRuleError(
                            "CONFLICT",
                            "service lease expired and requires administrative recovery",
                            rule="service_lease_expired",
                            details={"operation_id": operation_id, "expires_at": existing["expires_at"]},
                        )
                    self.conn.execute("COMMIT")
                    return existing
                rule = "service_lease_expired" if _parse(existing["expires_at"]) <= now else "service_lease_held"
                message = (
                    "service lease expired and requires administrative recovery"
                    if rule == "service_lease_expired"
                    else "operation is leased to another client run"
                )
                raise DishRuleError(
                    "CONFLICT", message, rule=rule,
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
                (lease_id, operation_id, op["task_gid"], principal.owner_id, principal.run_id,
                 stamp, stamp, _stamp(expiry)),
            )
            row = self.conn.execute("SELECT * FROM service_leases WHERE lease_id=?", (lease_id,)).fetchone()
            self.conn.execute("COMMIT")
            return row
        except DishRuleError:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as exc:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise DishRuleError("CONFLICT", "service lease acquisition collided", rule="service_lease_conflict") from exc
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def assert_owned(self, operation_id: str, principal: ServicePrincipal):
        row = self.active_for_operation(operation_id)
        if row is None:
            raise DishRuleError("CONFLICT", "operation has no active service lease", rule="service_lease_missing")
        now = self.now()
        if _parse(row["expires_at"]) <= now:
            raise DishRuleError(
                "CONFLICT", "service lease expired and requires administrative recovery",
                rule="service_lease_expired", details={"expires_at": row["expires_at"]},
            )
        if row["owner_id"] != principal.owner_id or row["run_id"] != principal.run_id:
            raise DishRuleError(
                "AGENT_MISMATCH", "service lease belongs to another client run",
                rule="service_lease_owner_mismatch",
                details={"owner_id": row["owner_id"], "run_id": row["run_id"]},
            )
        return row

    def renew(self, operation_id: str, principal: ServicePrincipal):
        row = self.assert_owned(operation_id, principal)
        now = self.now()
        expiry = now + timedelta(seconds=self.ttl_seconds)
        self.conn.execute(
            "UPDATE service_leases SET renewed_at=?, expires_at=? WHERE lease_id=?",
            (_stamp(now), _stamp(expiry), row["lease_id"]),
        )
        return self.active_for_operation(operation_id)

    def release(self, operation_id: str, principal: ServicePrincipal | None, *, reason: str, admin: bool = False):
        row = self.active_for_operation(operation_id)
        if row is None:
            return None
        if not admin:
            if principal is None:
                raise DishRuleError("INVALID_ARGUMENT", "service principal is required", rule="service_principal_required")
            self.assert_owned(operation_id, principal)
        now = _stamp(self.now())
        self.conn.execute(
            "UPDATE service_leases SET released_at=?, release_reason=? WHERE lease_id=?",
            (now, str(reason).strip() or "released", row["lease_id"]),
        )
        return self.conn.execute("SELECT * FROM service_leases WHERE lease_id=?", (row["lease_id"],)).fetchone()

    def release_for_handoff(self, operation_id: str, principal: ServicePrincipal, *, reason: str):
        op = self._operation(operation_id)
        if op["status"] not in {"open", "uncertain"}:
            return self.release(operation_id, principal, reason=reason)
        if op["phase"] not in {"await_verification", "held_evidence", "held_human"}:
            raise DishRuleError(
                "WRONG_STATE", "owner lease cannot be released before a workflow handoff",
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
                "WRONG_STATE", "owner lease cannot be released before durable completion markers",
                rule="service_lease_completion_pending",
            )
        return self.release(operation_id, principal, reason=reason)

    def release_terminal(self, operation_id: str, principal: ServicePrincipal, *, reason: str = "operation_terminal"):
        op = self._operation(operation_id)
        if op["status"] not in {"completed", "cancelled"}:
            raise DishRuleError(
                "WRONG_STATE", "task lock remains active until the operation is terminal",
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
                "WRONG_STATE", "task lock cannot release before all completion markers are durable",
                rule="service_lease_completion_pending",
            )
        return self.release(operation_id, principal, reason=reason)

    def admin_recover(self, operation_id: str, principal: ServicePrincipal, *, reason: str):
        row = self.active_for_operation(operation_id)
        if row is not None:
            if _parse(row["expires_at"]) > self.now():
                raise DishRuleError(
                    "CONFLICT", "active lease is not stale",
                    rule="service_lease_not_stale", details={"expires_at": row["expires_at"]},
                )
            self.release(operation_id, None, reason=f"admin recovery: {reason}", admin=True)
        return self.acquire(operation_id, principal)
