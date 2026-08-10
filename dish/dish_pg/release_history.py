"""Shared terminal-history / release-candidate boundary invariants."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

TERMINAL_HISTORY_IMPORT_KIND = "terminal-history-backfill-v1"
SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT = (
    "candidate-authority-v3+supplemental-terminal-history-v1"
)


def acquire_generation_release_gate(
    session: Session, *, generation_id: uuid.UUID
) -> models.AuthorityGeneration | None:
    """Serialize terminal-history application with candidate validation per generation.

    PostgreSQL holds the AuthorityGeneration row lock until the caller-owned transaction
    ends.  Other dialects still perform a fresh identity-map refresh so focused tests
    exercise the same re-read semantics without claiming PostgreSQL lock certification.
    """

    statement = select(models.AuthorityGeneration).where(
        models.AuthorityGeneration.generation_id == generation_id
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    statement = statement.execution_options(populate_existing=True)
    return session.scalar(statement)
