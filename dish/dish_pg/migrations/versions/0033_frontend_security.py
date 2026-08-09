"""Add frontend-only authentication and session support.

Revision ID: 0033_frontend_security
Revises: 0032_imported_operation_history
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_frontend_security"
down_revision = "0032_imported_operation_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "frontend_security_state",
        sa.Column("state_id", sa.BigInteger(), nullable=False),
        sa.Column("security_generation", sa.BigInteger(), nullable=False),
        sa.Column("password_verifier", sa.Text(), nullable=False),
        sa.Column("restore_fence_sha256", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state_id = 1", name=op.f("ck_frontend_security_state_singleton")),
        sa.CheckConstraint("security_generation > 0", name=op.f("ck_frontend_security_state_generation_positive")),
        sa.CheckConstraint("length(password_verifier) BETWEEN 20 AND 1024", name=op.f("ck_frontend_security_state_verifier_bounded")),
        sa.CheckConstraint("length(restore_fence_sha256) = 64", name=op.f("ck_frontend_security_state_restore_fence_hash_length")),
        sa.PrimaryKeyConstraint("state_id", name=op.f("pk_frontend_security_state")),
    )
    op.create_table(
        "frontend_sessions",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("token_verifier", sa.LargeBinary(length=32), nullable=False),
        sa.Column("security_generation", sa.BigInteger(), nullable=False),
        sa.Column("restore_fence_sha256", sa.String(length=64), nullable=False),
        sa.Column("peer_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("security_generation > 0", name=op.f("ck_frontend_sessions_generation_positive")),
        sa.CheckConstraint("length(restore_fence_sha256) = 64", name=op.f("ck_frontend_sessions_restore_fence_hash_length")),
        sa.CheckConstraint("expires_at > issued_at", name=op.f("ck_frontend_sessions_expiry_after_issue")),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_frontend_sessions")),
        sa.UniqueConstraint("token_verifier", name=op.f("uq_frontend_sessions_token_verifier")),
    )
    op.create_index("ix_frontend_sessions_expiry", "frontend_sessions", ["expires_at"], unique=False)
    op.create_index(
        "ix_frontend_sessions_live_generation",
        "frontend_sessions",
        ["security_generation"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "frontend_login_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("peer_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_after_seconds", sa.BigInteger(), nullable=True),
        sa.Column("peer_blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("global_blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("outcome IN ('success','failure','throttled')", name=op.f("ck_frontend_login_events_outcome_allowed")),
        sa.CheckConstraint(
            "(outcome = 'throttled' AND retry_after_seconds IS NOT NULL AND retry_after_seconds > 0) OR "
            "(outcome <> 'throttled' AND retry_after_seconds IS NULL)",
            name=op.f("ck_frontend_login_events_retry_matches_outcome"),
        ),
        sa.CheckConstraint(
            "peer_blocked_until IS NULL OR (outcome = 'failure' AND peer_blocked_until > occurred_at)",
            name=op.f("ck_frontend_login_events_peer_block_matches_failure"),
        ),
        sa.CheckConstraint(
            "global_blocked_until IS NULL OR (outcome = 'failure' AND global_blocked_until > occurred_at)",
            name=op.f("ck_frontend_login_events_global_block_matches_failure"),
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_frontend_login_events")),
    )
    op.create_index("ix_frontend_login_events_peer_time", "frontend_login_events", ["peer_digest", "occurred_at"], unique=False)
    op.create_index("ix_frontend_login_events_outcome_time", "frontend_login_events", ["outcome", "occurred_at"], unique=False)
    op.create_table(
        "frontend_security_audit",
        sa.Column("audit_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("security_generation", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("peer_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("detail_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint("security_generation > 0", name=op.f("ck_frontend_security_audit_generation_positive")),
        sa.CheckConstraint(
            "event_type IN ('login_success','login_failure','login_throttled','logout','session_expired',"
            "'session_revoked','password_provisioned','password_rotated','global_invalidation','restore_fence_rotated')",
            name=op.f("ck_frontend_security_audit_event_type_allowed"),
        ),
        sa.CheckConstraint("detail_code IS NULL OR length(trim(detail_code)) BETWEEN 1 AND 64", name=op.f("ck_frontend_security_audit_detail_code_bounded")),
        sa.PrimaryKeyConstraint("audit_id", name=op.f("pk_frontend_security_audit")),
    )
    op.create_index("ix_frontend_security_audit_time", "frontend_security_audit", ["occurred_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_frontend_security_audit_time", table_name="frontend_security_audit")
    op.drop_table("frontend_security_audit")
    op.drop_index("ix_frontend_login_events_outcome_time", table_name="frontend_login_events")
    op.drop_index("ix_frontend_login_events_peer_time", table_name="frontend_login_events")
    op.drop_table("frontend_login_events")
    op.drop_index("ix_frontend_sessions_live_generation", table_name="frontend_sessions")
    op.drop_index("ix_frontend_sessions_expiry", table_name="frontend_sessions")
    op.drop_table("frontend_sessions")
    op.drop_table("frontend_security_state")
