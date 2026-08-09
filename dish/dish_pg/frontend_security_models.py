"""Frontend-only authentication/session persistence.

These records authorize only the private browser read surface. They do not own
workflow legality, task content, placement, completion, or command authority.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class FrontendSecurityState(Base):
    __tablename__ = "frontend_security_state"

    state_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    password_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    restore_fence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("state_id = 1", name="singleton"),
        CheckConstraint("security_generation > 0", name="generation_positive"),
        CheckConstraint("length(password_verifier) BETWEEN 20 AND 1024", name="verifier_bounded"),
        CheckConstraint("length(restore_fence_sha256) = 64", name="restore_fence_hash_length"),
    )


class FrontendSession(Base):
    __tablename__ = "frontend_sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    token_verifier: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    security_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    restore_fence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    peer_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("security_generation > 0", name="generation_positive"),
        CheckConstraint("length(restore_fence_sha256) = 64", name="restore_fence_hash_length"),
        CheckConstraint("expires_at > issued_at", name="expiry_after_issue"),
        Index("ix_frontend_sessions_expiry", "expires_at"),
        Index(
            "ix_frontend_sessions_live_generation",
            "security_generation",
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )


class FrontendLoginEvent(Base):
    __tablename__ = "frontend_login_events"

    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    peer_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retry_after_seconds: Mapped[int | None] = mapped_column(BigInteger)
    peer_blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    global_blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("outcome IN ('success','failure','throttled')", name="outcome_allowed"),
        CheckConstraint(
            "(outcome = 'throttled' AND retry_after_seconds IS NOT NULL AND retry_after_seconds > 0) "
            "OR (outcome <> 'throttled' AND retry_after_seconds IS NULL)",
            name="retry_matches_outcome",
        ),
        CheckConstraint(
            "peer_blocked_until IS NULL OR (outcome = 'failure' AND peer_blocked_until > occurred_at)",
            name="peer_block_matches_failure",
        ),
        CheckConstraint(
            "global_blocked_until IS NULL OR (outcome = 'failure' AND global_blocked_until > occurred_at)",
            name="global_block_matches_failure",
        ),
        Index("ix_frontend_login_events_peer_time", "peer_digest", "occurred_at"),
        Index("ix_frontend_login_events_outcome_time", "outcome", "occurred_at"),
    )


class FrontendSecurityAudit(Base):
    __tablename__ = "frontend_security_audit"

    audit_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    security_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    peer_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    detail_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("security_generation > 0", name="generation_positive"),
        CheckConstraint(
            "event_type IN ('login_success','login_failure','login_throttled','logout','session_expired',"
            "'session_revoked','password_provisioned','password_rotated','global_invalidation','restore_fence_rotated')",
            name="event_type_allowed",
        ),
        CheckConstraint(
            "detail_code IS NULL OR length(trim(detail_code)) BETWEEN 1 AND 64",
            name="detail_code_bounded",
        ),
        Index("ix_frontend_security_audit_time", "occurred_at"),
    )
