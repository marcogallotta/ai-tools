"""Establish the Stage A migration lineage without target domain tables.

Revision ID: 0001_stage_a_baseline
Revises: None
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_stage_a_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stage 1 intentionally creates no target authority tables."""


def downgrade() -> None:
    """There is no target authority to remove in the baseline revision."""
