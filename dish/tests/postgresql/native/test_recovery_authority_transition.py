"""Native PostgreSQL certification for the rollback-burn recovery authority boundary."""
from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from sqlalchemy import select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import models
from dish_pg import stage6_models as release_models
from dish_pg.candidate_manifest import revalidate_candidate_manifest
from dish_pg.database import session_scope
from tests.support.postgresql.core import _next, core_db
from tests.support.postgresql.recovery_control import NOW, _setup

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_native_rollback_burn_requires_guarded_transition_and_is_immutable(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context, _epoch, candidate = _setup(session, ids, candidate_status="approved")
        candidate_id = candidate.candidate_id
        generation_id = context["generation_id"]

    engine = factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
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
                (NOW + timedelta(minutes=1), candidate_id),
            )
    finally:
        raw.close()

    burned_at = NOW + timedelta(minutes=1)
    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        approval = session.scalar(
            select(release_models.CutoverApproval).where(
                release_models.CutoverApproval.candidate_id == candidate_id
            )
        )
        manifest = session.scalar(
            select(manifest_models.ReleaseCandidateManifest).where(
                manifest_models.ReleaseCandidateManifest.candidate_id == candidate_id
            )
        )
        assert candidate is not None and approval is not None and manifest is not None
        revalidation = revalidate_candidate_manifest(
            session,
            uuid_factory=lambda: _next(ids),
            candidate=candidate,
            revalidated_at=burned_at,
        )
        assert revalidation.result == "matched"
        session.add(
            models.AuthorityActivation(
                activation_id=_next(ids),
                generation_id=generation_id,
                import_run_id=manifest.source_import_run_id,
                cutover_approval_id=str(approval.approval_id),
                legacy_bundle_id="native-rollback-burn",
                schema_head=candidate.schema_head,
                dish_release=candidate.dish_release,
                honest_release=candidate.honest_release,
                protocol_release=candidate.protocol_release,
                openapi_release=candidate.openapi_release,
                routing_release=candidate.routing_release,
                projection_epoch=candidate.projection_epoch_id,
                outcome="activated",
                rollback_burned_at=burned_at,
                recorded_at=burned_at,
            )
        )
        candidate.status = "activated"
        candidate.candidate_revision += 1
        candidate.terminal_at = burned_at
        session.flush()

    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        with pytest.raises(psycopg.errors.RaiseException, match="illegal release candidate transition"):
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
