"""Native PostgreSQL certification for the rollback-burn recovery authority boundary."""
from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from dish_pg import models
from dish_pg import stage6_models as release_models
from dish_pg.database import session_scope
from dish_pg.release import ALEMBIC_HEAD, ReleaseCandidateService
from tests.support.postgresql.core import (
    _bootstrap_registry,
    _import_one,
    _next,
    core_db,
)
from tests.support.postgresql.release import HASH_A
from tests.support.postgresql.stage8_cutover_evidence_gates import (
    _prepare_fenced_recertified_cutover,
)
from tests.support.postgresql.workflow import NOW

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_native_rollback_burn_requires_guarded_transition_and_is_immutable(core_db) -> None:
    factory, ids = core_db

    # The Stage 8 helper consumes a predecessor/import baseline that production would
    # already have durably established. Keep that boundary explicit in this native test.
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head=ALEMBIC_HEAD,
        )
        task = _import_one(session, ids, context)
        generation_id = context["generation_id"]
        generation = session.get(models.AuthorityGeneration, generation_id)
        assert generation is not None
        task_id = task.task_id
        dish_release = generation.dish_release

    # This second boundary is intentional: the independent raw PostgreSQL connection
    # below must observe the production-approved, fenced, and recertified candidate.
    with session_scope(factory) as session:
        _service, candidate_id, closure, run, fence = (
            _prepare_fenced_recertified_cutover(
                session,
                ids,
                context,
                task_id,
                dish_release=dish_release,
            )
        )
        closure_id = closure.closure_id
        cutover_run_id = run.cutover_run_id
        writer_identity = fence.target_identity

    engine = factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.driver_connection.autocommit = True
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="candidate activation lacks fresh matched manifest revalidation",
        ):
            raw.execute(
                """UPDATE release_candidates
                      SET status='activated',
                          candidate_revision=candidate_revision + 1,
                          terminal_at=%s
                    WHERE candidate_id=%s""",
                (NOW + timedelta(minutes=6), candidate_id),
            )
    finally:
        raw.close()

    burned_at = NOW + timedelta(minutes=6)
    with session_scope(factory) as session:
        service = ReleaseCandidateService(
            session, uuid_factory=lambda: _next(ids)
        )
        service.activate_authority(
            cutover_run_id=cutover_run_id,
            final_asana_closure_id=closure_id,
            activated_at=NOW + timedelta(minutes=5),
            required_writer_inventory={writer_identity},
        )
        activation = service.burn_rollback(
            cutover_run_id=cutover_run_id,
            legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
            burned_at=burned_at,
            required_writer_inventory={writer_identity},
        )
        candidate = service._candidate(candidate_id)
        cutover = session.get(release_models.CutoverRun, cutover_run_id)
        assert cutover is not None and cutover.state == "rollback_burned"
        assert candidate.status == "activated"
        assert activation.generation_id == generation_id
        assert activation.rollback_burned_at == burned_at
        assert activation.recorded_at == burned_at
        assert activation.cutover_approval_id

    raw = engine.raw_connection()
    try:
        raw.driver_connection.autocommit = True
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="release candidate identity is immutable",
        ):
            raw.execute(
                "UPDATE release_candidates SET source_release='tampered' WHERE candidate_id=%s",
                (candidate_id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable authority row"):
            raw.execute(
                "UPDATE authority_activations SET legacy_bundle_id='tampered' WHERE generation_id=%s",
                (generation_id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable authority row"):
            raw.execute(
                "DELETE FROM authority_activations WHERE generation_id=%s",
                (generation_id,),
            )
    finally:
        raw.close()
