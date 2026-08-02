from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.repositories import AuthorityRepository
from tests.support.postgresql.core import (
    HASH_A, NOW, _bootstrap_registry, _next, core_db,
)


def test_migration_and_contract_provenance_is_immutable(core_db) -> None:
    factory, ids = core_db
    migration_event_id = _next(ids)
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        AuthorityRepository(session).add_migration_event(
            models.AppliedMigrationEvent(
                migration_event_id=migration_event_id,
                generation_id=context["generation_id"],
                revision="0002_core_authority_model",
                predecessor_revision="0001_stage_a_baseline",
                migration_code_sha256=HASH_A,
                dish_release="dish-42619b9",
                initiator="stage2-test",
                outcome="applied",
                started_at=NOW,
                terminal_at=NOW,
                details={"database": "fixture"},
            )
        )

    with pytest.raises(IntegrityError, match="immutable authority row"):
        with session_scope(factory) as session:
            event_row = session.get(models.AppliedMigrationEvent, migration_event_id)
            assert event_row is not None
            event_row.details = {"database": "rewritten"}
            session.flush()

    with pytest.raises(IntegrityError, match="immutable authority row"):
        with session_scope(factory) as session:
            binding = session.get(
                models.HonestContractBinding, context["binding_id"]
            )
            assert binding is not None
            binding.provenance = {"resolved_by": "rewritten"}
            session.flush()


def test_only_one_active_generation_and_restore_transition_is_bound(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="pending")
        authority = AuthorityRepository(session)
        authority.activate_generation(
            generation_id=context["generation_id"],
            activation=models.AuthorityActivation(
                activation_id=_next(ids),
                generation_id=context["generation_id"],
                import_run_id=context["import_run_id"],
                cutover_approval_id="approval-1",
                legacy_bundle_id="bundle-1",
                schema_head="0002_core_authority_model",
                dish_release="dish-42619b9",
                honest_release="honest-1",
                protocol_release="protocol-1",
                openapi_release="openapi-1",
                routing_release="route-1",
                projection_epoch=_next(ids),
                outcome="activated",
                rollback_burned_at=NOW,
                recorded_at=NOW,
            ),
            at=NOW,
        )

    with factory() as session:
        active = session.scalar(
            select(models.AuthorityGeneration).where(
                models.AuthorityGeneration.status == "active"
            )
        )
        assert active is not None and active.generation_id == context["generation_id"]

    with pytest.raises(IntegrityError):
        with session_scope(factory) as session:
            session.add(
                models.AuthorityGeneration(
                    generation_id=_next(ids),
                    predecessor_generation_id=None,
                    creation_reason="initial_cutover",
                    external_restore_control_id=None,
                    schema_head="0002_core_authority_model",
                    dish_release="dish-duplicate",
                    status="active",
                    created_at=NOW,
                    retired_at=None,
                )
            )
            session.flush()
