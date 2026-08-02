"""Stage 6 release candidate, rehearsal, cutover, and admission authority.

Revision ID: 0005_release_cutover
Revises: 0004_transition_projection
"""
from __future__ import annotations

from alembic import op

from dish_pg.models import Base
from dish_pg.stage6_models import STAGE6_IMMUTABLE_TABLE_NAMES, STAGE6_TABLE_NAMES

revision = "0005_release_cutover"
down_revision = "0004_transition_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in STAGE6_TABLE_NAMES:
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=False)

    if bind.dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_reject_immutable_release_evidence()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable Stage 6 evidence row: %', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table_name in STAGE6_IMMUTABLE_TABLE_NAMES:
        op.execute(
            f"CREATE TRIGGER {table_name}_immutable_update "
            f"BEFORE UPDATE ON {table_name} FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_release_evidence()"
        )
        op.execute(
            f"CREATE TRIGGER {table_name}_immutable_delete "
            f"BEFORE DELETE ON {table_name} FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_release_evidence()"
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_release_candidate_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.candidate_id <> NEW.candidate_id
               OR OLD.generation_id <> NEW.generation_id
               OR OLD.source_import_batch_id <> NEW.source_import_batch_id
               OR OLD.shadow_baseline_id <> NEW.shadow_baseline_id
               OR OLD.projection_epoch_id <> NEW.projection_epoch_id
               OR OLD.source_release <> NEW.source_release
               OR OLD.source_commit <> NEW.source_commit
               OR OLD.ledger_through_commit <> NEW.ledger_through_commit
               OR OLD.schema_head <> NEW.schema_head
               OR OLD.dish_release <> NEW.dish_release
               OR OLD.honest_release <> NEW.honest_release
               OR OLD.protocol_release <> NEW.protocol_release
               OR OLD.openapi_release <> NEW.openapi_release
               OR OLD.routing_release <> NEW.routing_release
               OR OLD.created_at <> NEW.created_at THEN
                RAISE EXCEPTION 'release candidate identity is immutable';
            END IF;
            IF NEW.candidate_revision <> OLD.candidate_revision + 1 THEN
                RAISE EXCEPTION 'release candidate revision must advance exactly once';
            END IF;
            IF (OLD.status = 'assembling' AND NEW.status NOT IN ('validated','aborted'))
               OR (OLD.status = 'validated' AND NEW.status NOT IN ('approved','aborted'))
               OR (OLD.status = 'approved' AND NEW.status NOT IN ('activated','aborted'))
               OR OLD.status IN ('activated','aborted') THEN
                RAISE EXCEPTION 'illegal release candidate transition';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER release_candidates_transition_guard
        BEFORE UPDATE ON release_candidates FOR EACH ROW
        EXECUTE FUNCTION dish_validate_release_candidate_transition();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_writer_fence_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.fence_id <> NEW.fence_id
               OR OLD.candidate_id <> NEW.candidate_id
               OR OLD.target_identity <> NEW.target_identity
               OR OLD.mechanism <> NEW.mechanism
               OR OLD.manifest_sha256 <> NEW.manifest_sha256
               OR OLD.prepared_at <> NEW.prepared_at THEN
                RAISE EXCEPTION 'legacy writer fence identity is immutable';
            END IF;
            IF NEW.fence_revision <> OLD.fence_revision + 1 THEN
                RAISE EXCEPTION 'writer fence revision must advance exactly once';
            END IF;
            IF (OLD.state = 'prepared' AND NEW.state <> 'engaged')
               OR (OLD.state = 'engaged' AND NEW.state NOT IN ('verified','released'))
               OR (OLD.state = 'verified' AND NEW.state <> 'released')
               OR OLD.state = 'released' THEN
                RAISE EXCEPTION 'illegal writer fence transition';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER legacy_writer_fences_transition_guard
        BEFORE UPDATE ON legacy_writer_fences FOR EACH ROW
        EXECUTE FUNCTION dish_validate_writer_fence_transition();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_cutover_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.cutover_run_id <> NEW.cutover_run_id
               OR OLD.candidate_id <> NEW.candidate_id
               OR OLD.started_at <> NEW.started_at THEN
                RAISE EXCEPTION 'cutover run identity is immutable';
            END IF;
            IF NEW.state_revision <> OLD.state_revision + 1 THEN
                RAISE EXCEPTION 'cutover state revision must advance exactly once';
            END IF;
            IF (OLD.state = 'prepared' AND NEW.state NOT IN ('fenced','aborted'))
               OR (OLD.state = 'fenced' AND NEW.state NOT IN ('activated','aborted'))
               OR (OLD.state = 'activated' AND NEW.state NOT IN ('rollback_burned','aborted'))
               OR (OLD.state = 'rollback_burned' AND NEW.state <> 'admission_open')
               OR (OLD.state = 'admission_open' AND NEW.state <> 'first_admission_verified')
               OR (OLD.state = 'first_admission_verified' AND NEW.state <> 'completed')
               OR OLD.state IN ('completed','aborted') THEN
                RAISE EXCEPTION 'illegal cutover state transition';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER cutover_runs_transition_guard
        BEFORE UPDATE ON cutover_runs FOR EACH ROW
        EXECUTE FUNCTION dish_validate_cutover_transition();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_mutation_admission_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.generation_id <> NEW.generation_id
               OR OLD.candidate_id <> NEW.candidate_id THEN
                RAISE EXCEPTION 'mutation admission identity is immutable';
            END IF;
            IF NEW.control_revision <> OLD.control_revision + 1 THEN
                RAISE EXCEPTION 'mutation admission revision must advance exactly once';
            END IF;
            IF OLD.state <> 'closed' OR NEW.state <> 'open' THEN
                RAISE EXCEPTION 'mutation admission may open exactly once';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER mutation_admission_controls_transition_guard
        BEFORE UPDATE ON mutation_admission_controls FOR EACH ROW
        EXECUTE FUNCTION dish_validate_mutation_admission_transition();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_require_open_mutation_admission()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM mutation_admission_controls mac
                 WHERE mac.generation_id = NEW.generation_id
                   AND mac.state = 'closed'
            ) THEN
                RAISE EXCEPTION 'PostgreSQL mutation admission is closed';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER service_requests_stage6_admission_guard
        BEFORE INSERT ON service_requests FOR EACH ROW
        EXECUTE FUNCTION dish_require_open_mutation_admission();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for function_name in (
            "dish_require_open_mutation_admission",
            "dish_validate_mutation_admission_transition",
            "dish_validate_cutover_transition",
            "dish_validate_writer_fence_transition",
            "dish_validate_release_candidate_transition",
            "dish_reject_immutable_release_evidence",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {function_name}() CASCADE")
    for table_name in reversed(STAGE6_TABLE_NAMES):
        Base.metadata.tables[table_name].drop(bind=bind, checkfirst=False)
