"""Transactional persistence for the frontend-only security authority."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .frontend_security_models import (
    FrontendLoginEvent,
    FrontendSecurityAudit,
    FrontendSecurityState,
    FrontendSession,
)


class FrontendSecurityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def state(self, *, for_update: bool = False) -> FrontendSecurityState | None:
        statement = select(FrontendSecurityState).where(FrontendSecurityState.state_id == 1)
        if for_update:
            statement = statement.with_for_update()
        return self.session.execute(statement).scalar_one_or_none()

    def session_by_verifier(self, verifier: bytes, *, for_update: bool = False) -> FrontendSession | None:
        statement = select(FrontendSession).where(FrontendSession.token_verifier == verifier)
        if for_update:
            statement = statement.with_for_update()
        return self.session.execute(statement).scalar_one_or_none()

    def failure_counts(self, *, peer: bytes, since: datetime) -> tuple[int, int]:
        peer_count = self.session.execute(
            select(func.count()).select_from(FrontendLoginEvent).where(
                FrontendLoginEvent.outcome == "failure",
                FrontendLoginEvent.peer_digest == peer,
                FrontendLoginEvent.occurred_at > since,
            )
        ).scalar_one()
        global_count = self.session.execute(
            select(func.count()).select_from(FrontendLoginEvent).where(
                FrontendLoginEvent.outcome == "failure", FrontendLoginEvent.occurred_at > since
            )
        ).scalar_one()
        return int(peer_count), int(global_count)

    def active_block_deadline(self, *, peer: bytes, now: datetime) -> datetime | None:
        peer_deadline = self.session.execute(
            select(func.max(FrontendLoginEvent.peer_blocked_until)).where(
                FrontendLoginEvent.peer_digest == peer,
                FrontendLoginEvent.peer_blocked_until > now,
            )
        ).scalar_one_or_none()
        global_deadline = self.session.execute(
            select(func.max(FrontendLoginEvent.global_blocked_until)).where(
                FrontendLoginEvent.global_blocked_until > now
            )
        ).scalar_one_or_none()
        deadlines = [value for value in (peer_deadline, global_deadline) if value is not None]
        return max(deadlines) if deadlines else None

    def add_login_event(
        self, *, peer: bytes, outcome: str, now: datetime,
        retry_after_seconds: int | None = None,
        peer_blocked_until: datetime | None = None,
        global_blocked_until: datetime | None = None,
    ) -> None:
        self.session.add(FrontendLoginEvent(
            event_id=uuid.uuid4(), peer_digest=peer, outcome=outcome,
            occurred_at=now, retry_after_seconds=retry_after_seconds,
            peer_blocked_until=peer_blocked_until, global_blocked_until=global_blocked_until,
        ))

    def add_audit(
        self,
        *,
        event_type: str,
        now: datetime,
        generation: int,
        session_id: uuid.UUID | None = None,
        peer: bytes | None = None,
        detail_code: str | None = None,
    ) -> None:
        self.session.add(FrontendSecurityAudit(
            audit_id=uuid.uuid4(), event_type=event_type, occurred_at=now,
            security_generation=generation, session_id=session_id,
            peer_digest=peer, detail_code=detail_code,
        ))

    def create_session(
        self,
        *,
        verifier: bytes,
        generation: int,
        restore_fence_sha256: str,
        peer: bytes,
        issued_at: datetime,
        expires_at: datetime,
    ) -> FrontendSession:
        row = FrontendSession(
            session_id=uuid.uuid4(), token_verifier=verifier,
            security_generation=generation, restore_fence_sha256=restore_fence_sha256,
            peer_digest=peer, issued_at=issued_at, expires_at=expires_at, revoked_at=None,
        )
        self.session.add(row)
        return row

    def revoke(self, row: FrontendSession, *, now: datetime) -> bool:
        if row.revoked_at is not None:
            return False
        row.revoked_at = now
        return True

    def revoke_all(self, *, now: datetime) -> int:
        result = self.session.execute(
            update(FrontendSession).where(FrontendSession.revoked_at.is_(None)).values(revoked_at=now)
        )
        return int(result.rowcount or 0)

    def cleanup(self, *, now: datetime, login_retention: timedelta = timedelta(days=2), session_retention: timedelta = timedelta(days=14)) -> None:
        self.session.query(FrontendLoginEvent).filter(
            FrontendLoginEvent.occurred_at < now - login_retention
        ).delete(synchronize_session=False)
        self.session.query(FrontendSession).filter(
            FrontendSession.expires_at < now - session_retention
        ).delete(synchronize_session=False)

    def audit_count(self) -> int:
        return int(self.session.execute(select(func.count()).select_from(FrontendSecurityAudit)).scalar_one())
