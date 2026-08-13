"""Durable client/run leases layered over the operation task lock."""
from __future__ import annotations

import contextlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from dish_tool.errors import DishRuleError
from dish_tool.database import operation_run_revocation
from dish_tool.operation_execution import (
    operation_recovery_pending,
    unresolved_operation_executions,
)
from dish_tool.transactions import immediate_transaction, require_transaction

def _lease_transaction(
    conn: sqlite3.Connection, *, label: str, manage_transaction: bool
) -> contextlib.AbstractContextManager[None]:
    if manage_transaction:
        return immediate_transaction(conn, label)
    require_transaction(conn, operation=label.replace("_", " "))
    return contextlib.nullcontext()


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

    def by_id(self, lease_id: str):
        return self.conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()

    def active_for_task(self, task_gid: str):
        return self.conn.execute(
            "SELECT * FROM service_leases WHERE task_gid=? AND released_at IS NULL",
            (task_gid,),
        ).fetchone()

    def is_expired(self, row, *, at: datetime | None = None) -> bool:
        return _parse(row["expires_at"]) <= (self.now() if at is None else at)

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

    def _revocation(self, operation_id: str, principal: ServicePrincipal):
        return operation_run_revocation(
            self.conn,
            operation_id=operation_id,
            owner_id=principal.owner_id,
            run_id=principal.run_id,
        )

    def assert_not_revoked(
        self, operation_id: str, principal: ServicePrincipal
    ) -> None:
        revoked = self._revocation(operation_id, principal)
        if revoked is None:
            return
        raise DishRuleError(
            "AGENT_MISMATCH",
            "This Dish run has been killed.",
            rule="killed_run_revoked",
            details={
                "operation_id": operation_id,
                "revocation_id": revoked["revocation_id"],
                "revoked_at": revoked["revoked_at"],
            },
        )

    def _assert_owned_row(
        self,
        row,
        principal: ServicePrincipal,
        *,
        now: datetime,
    ):
        if row is not None:
            self.assert_not_revoked(str(row["operation_id"]), principal)
        if row is None:
            raise DishRuleError(
                "CONFLICT", "operation has no active service lease",
                rule="service_lease_missing",
            )
        if self.is_expired(row, at=now):
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

    def _assert_expired_actor_revival_safe(
        self,
        row,
        principal: ServicePrincipal,
        *,
        now: datetime,
        request_id: str | None = None,
    ):
        """Prove one expired actor lease may resume under the same durable run.

        Expiry opens a takeover window; it does not itself revoke the run.  This
        predicate is evaluated under the same SQLite writer transaction as the
        revival update so a replacement lineage and a same-run revival cannot
        both win.
        """
        if row is None:
            raise DishRuleError(
                "CONFLICT",
                "operation has no current service lease to revive",
                rule="service_lease_missing",
            )
        operation_id = str(row["operation_id"])
        self.assert_not_revoked(operation_id, principal)
        if row["lease_kind"] != "actor":
            raise DishRuleError(
                "CONFLICT",
                "only an actor lease can be revived by the same run",
                rule="service_lease_revival_kind_invalid",
                details={"lease_kind": row["lease_kind"]},
            )
        if not self.is_owned_by(row, principal):
            raise DishRuleError(
                "AGENT_MISMATCH",
                "service lease belongs to another client run",
                rule="service_lease_owner_mismatch",
                details={"owner_id": row["owner_id"], "run_id": row["run_id"]},
            )
        if not self.is_expired(row, at=now):
            return row

        operation = self._operation(operation_id)
        if operation["status"] != "open" or operation["phase"] == "terminal":
            raise DishRuleError(
                "AGENT_MISMATCH",
                "the durable operation was replaced or closed before lease revival",
                rule="service_lease_revival_superseded",
                details={
                    "operation_id": operation_id,
                    "status": operation["status"],
                    "phase": operation["phase"],
                },
            )

        later = self.conn.execute(
            """SELECT lease_id,owner_id,run_id,actor_attempt_seq FROM service_leases
                 WHERE operation_id=? AND lease_kind='actor' AND actor_attempt_seq>?
                 ORDER BY actor_attempt_seq LIMIT 1""",
            (operation_id, row["actor_attempt_seq"]),
        ).fetchone()
        replacement = self.conn.execute(
            """SELECT 'succession' AS kind FROM operation_successions WHERE source_operation_id=?
               UNION ALL
               SELECT 'safe_reclaim' FROM safe_reclaims WHERE source_operation_id=?
               LIMIT 1""",
            (operation_id, operation_id),
        ).fetchone()
        abandonment = self.conn.execute(
            """SELECT abandonment_id,status FROM abandonment_attempts
                 WHERE source_operation_id=? ORDER BY created_at DESC LIMIT 1""",
            (operation_id,),
        ).fetchone()
        if later is not None or replacement is not None or abandonment is not None:
            details = {"operation_id": operation_id}
            if later is not None:
                details["later_lease_id"] = later["lease_id"]
                details["later_run_id"] = later["run_id"]
            if replacement is not None:
                details["replacement_kind"] = replacement["kind"]
            if abandonment is not None:
                details["abandonment_id"] = abandonment["abandonment_id"]
                details["abandonment_status"] = abandonment["status"]
            raise DishRuleError(
                "AGENT_MISMATCH",
                "a replacement, abandonment, or later actor attempt superseded this run",
                rule="service_lease_revival_superseded",
                details=details,
            )

        claim = self.conn.execute(
            "SELECT claim_id,command FROM operation_execution_claims WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        executions = unresolved_operation_executions(self.conn, operation_id)
        requests = self.conn.execute(
            """SELECT request_id,command,status,resolved_at FROM service_requests
                 WHERE operation_id=?
                   AND (status='pending' OR (status='uncertain' AND resolved_at IS NULL))
                   AND (? IS NULL OR request_id<>?)
                 ORDER BY created_at""",
            (operation_id, request_id, request_id),
        ).fetchall()
        proposals = self.conn.execute(
            """SELECT proposal_id,status FROM semantic_proposals
                 WHERE operation_id=? AND status IN ('pending','approved','claimed')
                 ORDER BY created_at""",
            (operation_id,),
        ).fetchall()
        recovery_pending = operation_recovery_pending(self.conn, operation_id)
        if claim is not None or executions or requests or proposals or recovery_pending:
            details = {"operation_id": operation_id}
            if claim is not None:
                details["execution_claim"] = {
                    "claim_id": claim["claim_id"],
                    "command": claim["command"],
                }
            if executions:
                details["unresolved_executions"] = [dict(item) for item in executions]
            if requests:
                details["unresolved_requests"] = [dict(item) for item in requests]
            if proposals:
                details["incomplete_proposals"] = [dict(item) for item in proposals]
            if recovery_pending:
                details["operation_recovery_pending"] = True
            raise DishRuleError(
                "WRONG_STATE",
                "expired lease cannot revive while consequential or recovery state is unresolved",
                rule="service_lease_revival_recovery_required",
                details=details,
            )
        return row

    def revive_expired_actor(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        request_id: str | None = None,
        manage_transaction: bool = True,
        check_only: bool = False,
    ):
        """Atomically revive one expired actor lease for the exact same principal."""
        now = self.now()
        expiry = now + timedelta(seconds=self.ttl_seconds)
        with _lease_transaction(
            self.conn,
            label="revive_expired_actor_lease",
            manage_transaction=manage_transaction,
        ):
            row = self.active_for_operation(operation_id)
            row = self._assert_expired_actor_revival_safe(
                row, principal, now=now, request_id=request_id
            )
            if not self.is_expired(row, at=now) or check_only:
                return row
            cursor = self.conn.execute(
                """UPDATE service_leases SET renewed_at=?, expires_at=?
                     WHERE lease_id=? AND operation_id=? AND lease_kind='actor'
                       AND released_at IS NULL AND owner_id=? AND run_id=?
                       AND expires_at<=?""",
                (
                    _stamp(now),
                    _stamp(expiry),
                    row["lease_id"],
                    operation_id,
                    principal.owner_id,
                    principal.run_id,
                    _stamp(now),
                ),
            )
            if cursor.rowcount != 1:
                raise DishRuleError(
                    "CONFLICT",
                    "service lease authority changed before same-run revival",
                    rule="service_lease_conflict",
                    details={"operation_id": operation_id, "lease_id": row["lease_id"]},
                )
            revived = self.active_for_operation(operation_id)
            if revived is None or revived["lease_id"] != row["lease_id"]:
                raise DishRuleError(
                    "CONFLICT",
                    "service lease authority changed during same-run revival",
                    rule="service_lease_conflict",
                    details={"operation_id": operation_id, "lease_id": row["lease_id"]},
                )
            return revived

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
        if operation_recovery_pending(self.conn, row["operation_id"]):
            return False
        self._release_row(row, reason="terminal_lease_reaped", now=now)
        return True

    def acquire(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        lease_kind: str = "actor",
        context_cycle_id: str | None = None,
    ):
        if lease_kind not in {"actor", "admin_request"}:
            raise ValueError("unsupported service lease kind")
        if lease_kind == "admin_request" and context_cycle_id is not None:
            raise ValueError("admin request leases cannot carry Verification cycle context")
        now = self.now()
        expiry = now + timedelta(seconds=self.ttl_seconds)
        with immediate_transaction(self.conn, "acquire_service_lease"):
            op = self._operation(operation_id)
            self.assert_not_revoked(operation_id, principal)
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
                    existing_kind = existing["lease_kind"]
                    if existing_kind is not None and existing_kind != lease_kind:
                        raise DishRuleError(
                            "CONFLICT",
                            "active service lease has a different authority kind",
                            rule="service_lease_context_mismatch",
                            details={
                                "operation_id": operation_id,
                                "lease_kind": existing_kind,
                                "requested_lease_kind": lease_kind,
                            },
                        )
                    existing_cycle = existing["context_cycle_id"]
                    if (
                        existing_kind is not None
                        and context_cycle_id is not None
                        and existing_cycle != context_cycle_id
                    ):
                        raise DishRuleError(
                            "CONFLICT",
                            "active service lease is bound to a different Verification cycle",
                            rule="service_lease_context_mismatch",
                            details={
                                "operation_id": operation_id,
                                "context_cycle_id": existing_cycle,
                                "requested_context_cycle_id": context_cycle_id,
                            },
                        )
                    if self.is_expired(existing, at=now):
                        if lease_kind == "actor":
                            return self.revive_expired_actor(
                                operation_id, principal, manage_transaction=False
                            )
                        raise DishRuleError(
                            "CONFLICT",
                            "service lease expired and requires administrative recovery",
                            rule="service_lease_expired",
                            details={"operation_id": operation_id, "expires_at": existing["expires_at"]},
                        )
                    return existing
                expired = self.is_expired(existing, at=now)
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
            if lease_kind == "actor":
                actor_attempt_seq = self.conn.execute(
                    """SELECT COALESCE(MAX(actor_attempt_seq), 0) + 1
                         FROM service_leases
                        WHERE task_gid=? AND lease_kind='actor'""",
                    (op["task_gid"],),
                ).fetchone()[0]
            else:
                actor_attempt_seq = None

            lease_id = str(uuid.uuid4())
            stamp = _stamp(now)
            try:
                self.conn.execute(
                    """INSERT INTO service_leases(
                           lease_id,operation_id,task_gid,owner_id,run_id,
                           acquired_at,renewed_at,expires_at,lease_kind,
                           actor_attempt_seq,context_cycle_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (lease_id, operation_id, op["task_gid"], principal.owner_id,
                     principal.run_id, stamp, stamp, _stamp(expiry), lease_kind,
                     actor_attempt_seq, context_cycle_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DishRuleError(
                    "CONFLICT",
                    "service lease acquisition collided",
                    rule="service_lease_conflict",
                ) from exc
            row = self.conn.execute(
                "SELECT * FROM service_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
            return row

    def assert_owned(self, operation_id: str, principal: ServicePrincipal):
        return self._assert_owned_row(
            self.active_for_operation(operation_id), principal, now=self.now()
        )

    def assert_exact_uncertain_recovery(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        execution_id: str,
    ):
        """Authorize only recovery of one unresolved uncertain execution.

        A live actor lease remains owned by that actor.  Marco may execute the
        protocol recovery for the exact fenced execution, but this does not
        authorize any other admin mutation and does not transfer or release the
        lease.
        """

        now = self.now()
        row = self.active_for_operation(operation_id)
        if row is None:
            return None
        if self.is_expired(row, at=now):
            raise DishRuleError(
                "CONFLICT",
                "expired actor lease requires recover-lease first",
                rule="service_lease_expired",
                details={"expires_at": row["expires_at"]},
            )
        if self.is_owned_by(row, principal):
            return row
        execution = next(
            (
                row
                for row in unresolved_operation_executions(self.conn, operation_id)
                if row["execution_id"] == execution_id and row["status"] == "uncertain"
            ),
            None,
        )
        if execution is None:
            return self._assert_owned_row(row, principal, now=now)
        return row

    def renew(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        request_id: str | None = None,
        manage_transaction: bool = True,
    ):
        now = self.now()
        expiry = now + timedelta(seconds=self.ttl_seconds)
        with _lease_transaction(
            self.conn, label="renew_service_lease", manage_transaction=manage_transaction
        ):
            op = self._operation(operation_id)
            if op["status"] != "open":
                raise DishRuleError(
                    "WRONG_STATE",
                    "service lease cannot be renewed for a terminal operation",
                    rule="service_lease_operation_not_open",
                    details={"operation_id": operation_id, "status": op["status"]},
                )
            row = self.active_for_operation(operation_id)
            if (
                row is not None
                and self.is_owned_by(row, principal)
                and row["lease_kind"] == "actor"
                and self.is_expired(row, at=now)
            ):
                return self.revive_expired_actor(
                    operation_id,
                    principal,
                    request_id=request_id,
                    manage_transaction=False,
                )
            row = self._assert_owned_row(row, principal, now=now)
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
            return renewed

    def release(
        self,
        operation_id: str,
        principal: ServicePrincipal | None,
        *,
        reason: str,
        admin: bool = False,
    ):
        now = self.now()
        with immediate_transaction(self.conn, "release_service_lease"):
            row = self.active_for_operation(operation_id)
            if row is None:
                return None
            if not admin:
                if principal is None:
                    raise DishRuleError(
                        "INVALID_ARGUMENT", "service principal is required",
                        rule="service_principal_required",
                    )
                self._assert_owned_row(row, principal, now=now)
            released = self._release_row(row, reason=reason, now=now)
            return released

    def release_for_handoff(
        self, operation_id: str, principal: ServicePrincipal, *, reason: str
    ):
        now = self.now()
        with immediate_transaction(self.conn, "release_for_handoff"):
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
            if operation_recovery_pending(self.conn, operation_id):
                raise DishRuleError(
                    "WRONG_STATE",
                    "owner lease cannot be released before durable completion markers",
                    rule="service_lease_completion_pending",
                )
            released = self._release_row(row, reason=reason, now=now)
            return released

    def release_after_exact_recovery_handoff(
        self,
        operation_id: str,
        *,
        execution_id: str,
        lease_id: str,
        reason: str,
    ):
        """Release only the actor lease that fenced one recovered handoff.

        The caller captures both identifiers while authorizing Marco's exact
        uncertain-execution recovery. This writer transaction then proves the
        same execution is durably resolved, no mutation claim or incomplete
        workflow evidence remains, and the operation is at a role handoff before
        releasing that exact pre-existing lease row. A replacement lease is
        never touched.
        """

        now = self.now()
        with immediate_transaction(self.conn, "release_after_exact_recovery_handoff"):
            operation = self._operation(operation_id)
            execution = self.conn.execute(
                """SELECT * FROM operation_executions
                     WHERE execution_id=? AND operation_id=?""",
                (execution_id, operation_id),
            ).fetchone()
            if (
                execution is None
                or execution["status"] != "completed"
                or execution["resolved_at"] is None
                or execution["resolution_evidence_json"] is None
            ):
                raise DishRuleError(
                    "WRONG_STATE",
                    "actor lease cannot be released before exact recovery is durably resolved",
                    rule="service_exact_recovery_incomplete",
                    details={"operation_id": operation_id, "execution_id": execution_id},
                )
            if execution["command"] not in {"prepare", "reject"}:
                raise DishRuleError(
                    "WRONG_STATE",
                    "exact recovery did not originate a supported role handoff",
                    rule="service_exact_recovery_handoff_invalid",
                    details={"command": execution["command"]},
                )
            if operation["status"] != "open" or operation["phase"] not in {
                "await_verification",
                "held_evidence",
                "held_human",
            }:
                raise DishRuleError(
                    "WRONG_STATE",
                    "exact recovery did not finish at a role handoff",
                    rule="service_exact_recovery_handoff_invalid",
                    details={
                        "status": operation["status"],
                        "phase": operation["phase"],
                    },
                )
            active_claim = self.conn.execute(
                "SELECT 1 FROM operation_execution_claims WHERE operation_id=? LIMIT 1",
                (operation_id,),
            ).fetchone()
            unresolved_execution = next(
                (
                    row
                    for row in unresolved_operation_executions(self.conn, operation_id)
                    if row["status"] == "uncertain"
                ),
                None,
            )
            if (
                active_claim
                or unresolved_execution
                or operation_recovery_pending(self.conn, operation_id)
            ):
                raise DishRuleError(
                    "WRONG_STATE",
                    "actor lease cannot be released before recovered workflow evidence is coherent",
                    rule="service_lease_completion_pending",
                    details={"operation_id": operation_id, "execution_id": execution_id},
                )

            lease = self.conn.execute(
                "SELECT * FROM service_leases WHERE lease_id=? AND operation_id=?",
                (lease_id, operation_id),
            ).fetchone()
            if lease is None:
                raise DishRuleError(
                    "CONFLICT",
                    "exact recovery lease evidence is missing",
                    rule="service_lease_conflict",
                    details={"operation_id": operation_id, "lease_id": lease_id},
                )
            if _parse(lease["acquired_at"]) > _parse(execution["created_at"]):
                raise DishRuleError(
                    "CONFLICT",
                    "active lease was not the lease that fenced the uncertain execution",
                    rule="service_lease_conflict",
                    details={"operation_id": operation_id, "lease_id": lease_id},
                )
            if lease["released_at"] is not None:
                active = self.active_for_operation(operation_id)
                if active is not None and active["lease_id"] != lease_id:
                    raise DishRuleError(
                        "CONFLICT",
                        "a replacement lease became active after exact recovery",
                        rule="service_lease_conflict",
                        details={
                            "operation_id": operation_id,
                            "expected_lease_id": lease_id,
                            "active_lease_id": active["lease_id"],
                        },
                    )
                return None
            active = self.active_for_operation(operation_id)
            if active is None or active["lease_id"] != lease_id:
                raise DishRuleError(
                    "CONFLICT",
                    "active lease changed after exact recovery authorization",
                    rule="service_lease_conflict",
                    details={"operation_id": operation_id, "lease_id": lease_id},
                )
            released = self._release_row(lease, reason=reason, now=now)
            return released

    def release_terminal(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str = "operation_terminal",
    ):
        now = self.now()
        with immediate_transaction(self.conn, "release_terminal"):
            op = self._operation(operation_id)
            row = self.active_for_operation(operation_id)
            if row is None and op["status"] in {"completed", "cancelled"}:
                # A later operation may have safely reaped this cleanup tail
                # after terminal workflow commit but before this request's own
                # response bookkeeping reached lease release.
                return None
            row = self._assert_owned_row(row, principal, now=now)
            if op["status"] not in {"completed", "cancelled"}:
                raise DishRuleError(
                    "WRONG_STATE",
                    "task lock remains active until the operation is terminal",
                    rule="service_task_lock_active",
                )
            if operation_recovery_pending(self.conn, operation_id):
                raise DishRuleError(
                    "WRONG_STATE",
                    "task lock cannot release before all completion markers are durable",
                    rule="service_lease_completion_pending",
                )
            released = self._release_row(row, reason=reason, now=now)
            return released

    def admin_recover(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
        manage_transaction: bool = True,
    ):
        del principal
        clean_reason = str(reason or "").strip()
        if clean_reason.startswith("<") and clean_reason.endswith(">"):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "lease recovery reason still contains the unfilled command placeholder",
                rule="lease_recovery_reason_placeholder",
            )
        now = self.now()
        with _lease_transaction(
            self.conn, label="recover_service_lease", manage_transaction=manage_transaction
        ):
            self._operation(operation_id)
            row = self.active_for_operation(operation_id)
            if row is None:
                return None
            if not self.is_expired(row, at=now):
                raise DishRuleError(
                    "CONFLICT", "active lease is not stale",
                    rule="service_lease_not_stale",
                    details={"expires_at": row["expires_at"]},
                )
            released = self._release_row(
                row, reason=f"admin recovery: {reason}", now=now
            )
            return released

    def admin_expire_selected(
        self,
        lease_id: str,
        *,
        reason: str,
        manage_transaction: bool = True,
    ):
        """Release one exact active lease without changing any workflow authority."""

        now = self.now()
        with _lease_transaction(
            self.conn, label="expire_service_lease", manage_transaction=manage_transaction
        ):
            row = self.by_id(lease_id)
            if row is None:
                raise DishRuleError(
                    "NOT_FOUND",
                    "service lease not found",
                    rule="service_lease_not_found",
                    details={"lease_id": lease_id},
                )
            if row["released_at"] is not None:
                return row, False
            released = self._release_row(row, reason=reason, now=now)
            return released, True
