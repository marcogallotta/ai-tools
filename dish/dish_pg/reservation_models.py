"""Exact first-request reservation authority for PostgreSQL cutover."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class FirstRequestReservation(Base):
    __tablename__ = "first_request_reservations"

    reservation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    cutover_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    command_name: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(256), nullable=False)
    principal_class: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    canonical_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    reservation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["candidate_id", "generation_id"],
            ["release_candidates.candidate_id", "release_candidates.generation_id"],
            ondelete="RESTRICT",
            name="fk_first_request_reservation_candidate_generation",
        ),
        ForeignKeyConstraint(
            ["cutover_run_id", "candidate_id"],
            ["cutover_runs.cutover_run_id", "cutover_runs.candidate_id"],
            ondelete="RESTRICT",
            name="fk_first_request_reservation_cutover_candidate",
        ),
        ForeignKeyConstraint(
            ["plan_id", "cutover_run_id", "request_id", "command_name"],
            [
                "first_admission_plans.plan_id",
                "first_admission_plans.cutover_run_id",
                "first_admission_plans.request_id",
                "first_admission_plans.command_name",
            ],
            ondelete="RESTRICT",
            name="fk_first_request_reservation_exact_plan",
        ),
        ForeignKeyConstraint(
            ["generation_id", "owner_id", "run_id"],
            ["service_runs.generation_id", "service_runs.owner_id", "service_runs.run_id"],
            ondelete="RESTRICT",
            name="fk_first_request_reservation_exact_run_owner",
        ),
        CheckConstraint("length(trim(command_name)) > 0", name="command_nonblank"),
        CheckConstraint("length(trim(owner_id)) > 0", name="owner_nonblank"),
        CheckConstraint(
            "principal_class IN ('agent','admin','verification','service')",
            name="principal_class_allowed",
        ),
        CheckConstraint(
            "length(canonical_payload_sha256) = 64", name="payload_hash_length"
        ),
        CheckConstraint(
            "state IN ('reserved','consumed','cancelled')", name="state_allowed"
        ),
        CheckConstraint("reservation_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(state = 'reserved' AND consumed_at IS NULL) OR "
            "(state = 'consumed' AND consumed_at IS NOT NULL) OR "
            "(state = 'cancelled' AND consumed_at IS NULL)",
            name="state_time_consistent",
        ),
        UniqueConstraint(
            "generation_id", "candidate_id", name="uq_first_request_reservation_authority"
        ),
    )
