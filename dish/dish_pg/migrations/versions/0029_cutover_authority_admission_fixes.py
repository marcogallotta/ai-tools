"""Enforce cutover lineage, legal initial states, and isolated first admission.

Revision ID: 0029_cutover_authority_admission_fixes
Revises: 0028_consumed_first_request_open_admission
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_cutover_authority_admission_fixes"
down_revision = "0028_consumed_first_request_open_admission"
branch_labels = None
depends_on = None


_DEPENDENCY_SPECS = (
    (
        "source_import_batches",
        "uq_source_import_batch_generation_identity",
        "source_import_batch_id",
        "import_batch_id",
        "fk_release_candidate_import_generation",
    ),
    (
        "shadow_baselines",
        "uq_shadow_baseline_generation_identity",
        "shadow_baseline_id",
        "shadow_baseline_id",
        "fk_release_candidate_shadow_generation",
    ),
    (
        "projection_epochs",
        "uq_projection_epoch_generation_identity",
        "projection_epoch_id",
        "projection_epoch_id",
        "fk_release_candidate_projection_generation",
    ),
)


def _preflight_populated_data() -> None:
    context = op.get_context()
    if context.as_sql:
        return
    bind = op.get_bind()
    mismatch_counts: dict[str, int] = {}
    for table, _unique_name, candidate_column, dependency_column, _fk_name in _DEPENDENCY_SPECS:
        mismatch_counts[table] = int(
            bind.execute(
                sa.text(
                    f"""
                    SELECT count(*)
                      FROM release_candidates candidate
                      JOIN {table} dependency
                        ON dependency.{dependency_column} = candidate.{candidate_column}
                     WHERE dependency.generation_id <> candidate.generation_id
                    """
                )
            ).scalar_one()
        )
    mismatched = {table: count for table, count in mismatch_counts.items() if count}
    if mismatched:
        details = ", ".join(f"{table}={count}" for table, count in sorted(mismatched.items()))
        raise RuntimeError(
            "release-candidate dependency generations conflict with populated lineage; "
            f"refusing to rewrite authority ({details})"
        )

    unsafe_open_controls = int(
        bind.execute(
            sa.text(
                """
                SELECT count(*)
                  FROM mutation_admission_controls control
                  LEFT JOIN release_candidates candidate
                    ON candidate.candidate_id = control.candidate_id
                   AND candidate.generation_id = control.generation_id
                  LEFT JOIN cutover_runs run
                    ON run.candidate_id = control.candidate_id
                  LEFT JOIN first_request_reservations reservation
                    ON reservation.candidate_id = control.candidate_id
                   AND reservation.generation_id = control.generation_id
                 WHERE control.state = 'open'
                   AND (
                        candidate.candidate_id IS NULL
                        OR run.cutover_run_id IS NULL
                        OR run.state NOT IN ('first_admission_verified','completed')
                        OR control.control_revision < 2
                        OR reservation.reservation_id IS NULL
                        OR reservation.state <> 'consumed'
                        OR reservation.reservation_revision < 2
                   )
                """
            )
        ).scalar_one()
    )
    if unsafe_open_controls:
        raise RuntimeError(
            "open mutation admission lacks verified first-admission authority; "
            "close or reconcile the populated cutover state before upgrading"
        )


def _create_generation_constraints() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table, unique_name, _candidate_column, dependency_column, _fk_name in _DEPENDENCY_SPECS:
            with op.batch_alter_table(table) as batch:
                batch.create_unique_constraint(unique_name, [dependency_column, "generation_id"])
        with op.batch_alter_table("release_candidates") as batch:
            for table, _unique_name, candidate_column, dependency_column, fk_name in _DEPENDENCY_SPECS:
                batch.create_foreign_key(
                    fk_name,
                    table,
                    [candidate_column, "generation_id"],
                    [dependency_column, "generation_id"],
                    ondelete="RESTRICT",
                )
        return

    for table, unique_name, _candidate_column, dependency_column, _fk_name in _DEPENDENCY_SPECS:
        op.create_unique_constraint(unique_name, table, [dependency_column, "generation_id"])
    for table, _unique_name, candidate_column, dependency_column, fk_name in _DEPENDENCY_SPECS:
        op.create_foreign_key(
            fk_name,
            "release_candidates",
            table,
            [candidate_column, "generation_id"],
            [dependency_column, "generation_id"],
            ondelete="RESTRICT",
        )


def _drop_generation_constraints() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("release_candidates") as batch:
            for _table, _unique_name, _candidate_column, _dependency_column, fk_name in reversed(
                _DEPENDENCY_SPECS
            ):
                batch.drop_constraint(fk_name, type_="foreignkey")
        for table, unique_name, _candidate_column, _dependency_column, _fk_name in reversed(
            _DEPENDENCY_SPECS
        ):
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(unique_name, type_="unique")
        return

    for _table, _unique_name, _candidate_column, _dependency_column, fk_name in reversed(
        _DEPENDENCY_SPECS
    ):
        op.drop_constraint(fk_name, "release_candidates", type_="foreignkey")
    for table, unique_name, _candidate_column, _dependency_column, _fk_name in reversed(
        _DEPENDENCY_SPECS
    ):
        op.drop_constraint(unique_name, table, type_="unique")


def _install_postgresql_initial_state_guards() -> None:
    specs = (
        (
            "release_candidates",
            "release_candidates_initial_state_guard",
            "dish_validate_release_candidate_insert",
            "NEW.status <> 'assembling' OR NEW.candidate_revision <> 1",
            "release candidate must initially be assembling at revision 1",
        ),
        (
            "mutation_admission_controls",
            "mutation_admission_controls_initial_state_guard",
            "dish_validate_mutation_admission_insert",
            "NEW.state <> 'closed' OR NEW.control_revision <> 1",
            "mutation admission control must initially be closed at revision 1",
        ),
        (
            "first_request_reservations",
            "first_request_reservations_initial_state_guard",
            "dish_validate_first_request_reservation_insert",
            "NEW.state <> 'reserved' OR NEW.reservation_revision <> 1",
            "first-request reservation must initially be reserved at revision 1",
        ),
    )
    for table_name, trigger_name, function_name, condition, message in specs:
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {function_name}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF {condition} THEN
                    RAISE EXCEPTION '{message}';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON {table_name} FOR EACH ROW
            EXECUTE FUNCTION {function_name}()
            """
        )


def _install_sqlite_initial_state_guards() -> None:
    op.execute(
        """
        CREATE TRIGGER release_candidates_initial_state_guard
        BEFORE INSERT ON release_candidates
        WHEN NEW.status <> 'assembling' OR NEW.candidate_revision <> 1
        BEGIN
            SELECT RAISE(ABORT, 'release candidate must initially be assembling at revision 1');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER mutation_admission_controls_initial_state_guard
        BEFORE INSERT ON mutation_admission_controls
        WHEN NEW.state <> 'closed' OR NEW.control_revision <> 1
        BEGIN
            SELECT RAISE(ABORT, 'mutation admission control must initially be closed at revision 1');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER first_request_reservations_initial_state_guard
        BEFORE INSERT ON first_request_reservations
        WHEN NEW.state <> 'reserved' OR NEW.reservation_revision <> 1
        BEGIN
            SELECT RAISE(ABORT, 'first-request reservation must initially be reserved at revision 1');
        END
        """
    )


def _install_postgresql_transition_guard() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_mutation_admission_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.generation_id <> NEW.generation_id THEN
                RAISE EXCEPTION 'mutation admission generation identity is immutable';
            END IF;
            IF NEW.control_revision <> OLD.control_revision + 1 THEN
                RAISE EXCEPTION 'mutation admission revision must advance exactly once';
            END IF;
            IF OLD.candidate_id <> NEW.candidate_id THEN
                IF OLD.state <> 'closed' OR NEW.state <> 'closed'
                   OR OLD.opened_at IS NOT NULL OR NEW.opened_at IS NOT NULL
                   OR NOT EXISTS (
                        SELECT 1 FROM release_candidates old_candidate
                         WHERE old_candidate.candidate_id = OLD.candidate_id
                           AND old_candidate.generation_id = OLD.generation_id
                           AND old_candidate.status = 'aborted'
                   )
                   OR NOT EXISTS (
                        SELECT 1 FROM release_candidates new_candidate
                         WHERE new_candidate.candidate_id = NEW.candidate_id
                           AND new_candidate.generation_id = NEW.generation_id
                           AND new_candidate.status = 'assembling'
                   )
                   OR EXISTS (
                        SELECT 1 FROM authority_activations activation
                         WHERE activation.generation_id = OLD.generation_id
                           AND activation.outcome = 'activated'
                   ) THEN
                    RAISE EXCEPTION 'mutation admission candidate may rebind only after exact pre-burn abort';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.state <> 'closed' OR NEW.state <> 'open' THEN
                RAISE EXCEPTION 'mutation admission may open exactly once';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                  FROM cutover_runs run
                  JOIN first_request_reservations reservation
                    ON reservation.cutover_run_id = run.cutover_run_id
                   AND reservation.candidate_id = run.candidate_id
                 WHERE run.candidate_id = NEW.candidate_id
                   AND run.state = 'first_admission_verified'
                   AND reservation.generation_id = NEW.generation_id
                   AND reservation.state = 'consumed'
            ) THEN
                RAISE EXCEPTION 'mutation admission opens only after verified first admission';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )


def _install_postgresql_admission_function(*, isolated_first_request: bool) -> None:
    if isolated_first_request:
        body = """
            IF EXISTS (
                SELECT 1 FROM legacy_request_tombstones tombstone
                 WHERE tombstone.request_id = NEW.request_id
            ) THEN
                RAISE EXCEPTION 'request identity is reserved by legacy authority';
            END IF;

            SELECT candidate_id, state
              INTO control_candidate, control_state
              FROM mutation_admission_controls
             WHERE generation_id = NEW.generation_id
             FOR UPDATE;
            IF NOT FOUND THEN
                IF EXISTS (
                    SELECT 1 FROM release_candidates candidate
                     WHERE candidate.generation_id = NEW.generation_id
                ) THEN
                    RAISE EXCEPTION 'PostgreSQL mutation admission is closed';
                END IF;
                RETURN NEW;
            END IF;

            SELECT * INTO reservation
              FROM first_request_reservations
             WHERE generation_id = NEW.generation_id
               AND candidate_id = control_candidate
             FOR UPDATE;
            IF NOT FOUND THEN
                IF control_state = 'open' THEN
                    RAISE EXCEPTION 'open mutation admission lacks a verified consumed first request';
                END IF;
                RAISE EXCEPTION 'PostgreSQL mutation admission is closed pending first-request gate';
            END IF;

            SELECT state INTO cutover_state
              FROM cutover_runs
             WHERE cutover_run_id = reservation.cutover_run_id
               AND candidate_id = control_candidate;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'PostgreSQL mutation admission lacks exact cutover authority';
            END IF;

            IF control_state = 'open' THEN
                IF reservation.state <> 'consumed'
                   OR cutover_state NOT IN ('first_admission_verified', 'completed') THEN
                    RAISE EXCEPTION 'open mutation admission lacks a verified consumed first request';
                END IF;
                RETURN NEW;
            END IF;
            IF control_state <> 'closed' THEN
                RAISE EXCEPTION 'PostgreSQL mutation admission is closed';
            END IF;
            IF cutover_state <> 'admission_open' THEN
                RAISE EXCEPTION 'PostgreSQL mutation admission is closed pending first-request gate';
            END IF;
            IF reservation.state <> 'reserved' THEN
                RAISE EXCEPTION 'PostgreSQL mutation admission is closed pending first-admission verification';
            END IF;
            IF reservation.request_id IS DISTINCT FROM NEW.request_id
               OR reservation.command_name IS DISTINCT FROM NEW.command_name
               OR reservation.owner_id IS DISTINCT FROM NEW.owner_id
               OR reservation.principal_class IS DISTINCT FROM NEW.principal_class
               OR reservation.run_id IS DISTINCT FROM NEW.run_id
               OR reservation.canonical_payload_sha256 IS DISTINCT FROM NEW.canonical_payload_sha256 THEN
                RAISE EXCEPTION 'first PostgreSQL mutation does not match the reserved request';
            END IF;

            UPDATE first_request_reservations
               SET state = 'consumed',
                   reservation_revision = reservation_revision + 1,
                   consumed_at = NEW.admitted_at
             WHERE reservation_id = reservation.reservation_id;
            RETURN NEW;
        """
    else:
        body = """
            IF EXISTS (
                SELECT 1 FROM legacy_request_tombstones tombstone
                 WHERE tombstone.request_id = NEW.request_id
            ) THEN
                RAISE EXCEPTION 'request identity is reserved by legacy authority';
            END IF;
            SELECT candidate_id, state
              INTO control_candidate, control_state
              FROM mutation_admission_controls
             WHERE generation_id = NEW.generation_id;
            IF NOT FOUND THEN
                RETURN NEW;
            END IF;
            IF control_state <> 'open' THEN
                RAISE EXCEPTION 'PostgreSQL mutation admission is closed';
            END IF;
            SELECT * INTO reservation
              FROM first_request_reservations
             WHERE generation_id = NEW.generation_id
               AND candidate_id = control_candidate
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'open mutation admission lacks an exact first-request reservation';
            END IF;
            IF reservation.state = 'consumed' THEN
                RETURN NEW;
            ELSIF reservation.state <> 'reserved' THEN
                RAISE EXCEPTION 'first PostgreSQL request reservation is not consumable';
            END IF;
            IF reservation.request_id IS DISTINCT FROM NEW.request_id
               OR reservation.command_name IS DISTINCT FROM NEW.command_name
               OR reservation.owner_id IS DISTINCT FROM NEW.owner_id
               OR reservation.principal_class IS DISTINCT FROM NEW.principal_class
               OR reservation.run_id IS DISTINCT FROM NEW.run_id
               OR reservation.canonical_payload_sha256 IS DISTINCT FROM NEW.canonical_payload_sha256 THEN
                RAISE EXCEPTION 'first PostgreSQL mutation does not match the reserved request';
            END IF;
            UPDATE first_request_reservations
               SET state = 'consumed',
                   reservation_revision = reservation_revision + 1,
                   consumed_at = NEW.admitted_at
             WHERE reservation_id = reservation.reservation_id;
            RETURN NEW;
        """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION dish_require_open_mutation_admission()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            control_candidate UUID;
            control_state VARCHAR(16);
            cutover_state VARCHAR(32);
            reservation first_request_reservations%ROWTYPE;
        BEGIN
            {body}
        END;
        $$;
        """
    )


def _install_sqlite_verified_open_guard() -> None:
    op.execute(
        """
        CREATE TRIGGER mutation_admission_controls_verified_open_guard
        BEFORE UPDATE ON mutation_admission_controls
        WHEN NEW.state = 'open' AND (
            OLD.state <> 'closed'
            OR NEW.generation_id <> OLD.generation_id
            OR NEW.candidate_id <> OLD.candidate_id
            OR NEW.control_revision <> OLD.control_revision + 1
            OR NOT EXISTS (
                SELECT 1
                  FROM cutover_runs run
                  JOIN first_request_reservations reservation
                    ON reservation.cutover_run_id = run.cutover_run_id
                   AND reservation.candidate_id = run.candidate_id
                 WHERE run.candidate_id = NEW.candidate_id
                   AND run.state = 'first_admission_verified'
                   AND reservation.generation_id = NEW.generation_id
                   AND reservation.state = 'consumed'
            )
        )
        BEGIN
            SELECT RAISE(ABORT,
                'mutation admission opens only after verified first admission');
        END
        """
    )


def _install_sqlite_admission_guard() -> None:
    op.execute("DROP TRIGGER IF EXISTS service_requests_stage6_admission_guard")
    op.execute(
        """
        CREATE TRIGGER service_requests_stage6_admission_guard
        BEFORE INSERT ON service_requests
        WHEN EXISTS (
            SELECT 1 FROM release_candidates candidate
             WHERE candidate.generation_id = NEW.generation_id
        )
        AND NOT EXISTS (
            SELECT 1
              FROM mutation_admission_controls control
              JOIN first_request_reservations reservation
                ON reservation.generation_id = control.generation_id
               AND reservation.candidate_id = control.candidate_id
              JOIN cutover_runs run
                ON run.cutover_run_id = reservation.cutover_run_id
               AND run.candidate_id = control.candidate_id
             WHERE control.generation_id = NEW.generation_id
               AND (
                    (
                        control.state = 'open'
                        AND reservation.state = 'consumed'
                        AND run.state IN ('first_admission_verified', 'completed')
                    )
                    OR (
                        control.state = 'closed'
                        AND reservation.state = 'reserved'
                        AND run.state = 'admission_open'
                        AND reservation.request_id = NEW.request_id
                        AND reservation.command_name = NEW.command_name
                        AND reservation.owner_id = NEW.owner_id
                        AND reservation.principal_class = NEW.principal_class
                        AND reservation.run_id = NEW.run_id
                        AND reservation.canonical_payload_sha256 = NEW.canonical_payload_sha256
                    )
               )
        )
        BEGIN
            SELECT RAISE(ABORT, 'PostgreSQL mutation admission is closed');
        END
        """
    )


def upgrade() -> None:
    _preflight_populated_data()
    _create_generation_constraints()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _install_postgresql_initial_state_guards()
        _install_postgresql_transition_guard()
        _install_postgresql_admission_function(isolated_first_request=True)
    elif bind.dialect.name == "sqlite":
        _install_sqlite_initial_state_guards()
        _install_sqlite_verified_open_guard()
        _install_sqlite_admission_guard()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for trigger_name, table_name in (
            ("release_candidates_initial_state_guard", "release_candidates"),
            ("mutation_admission_controls_initial_state_guard", "mutation_admission_controls"),
            ("first_request_reservations_initial_state_guard", "first_request_reservations"),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
        for function_name in (
            "dish_validate_release_candidate_insert",
            "dish_validate_mutation_admission_insert",
            "dish_validate_first_request_reservation_insert",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_mutation_admission_transition()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.generation_id <> NEW.generation_id THEN
                    RAISE EXCEPTION 'mutation admission generation identity is immutable';
                END IF;
                IF NEW.control_revision <> OLD.control_revision + 1 THEN
                    RAISE EXCEPTION 'mutation admission revision must advance exactly once';
                END IF;
                IF OLD.candidate_id <> NEW.candidate_id THEN
                    IF OLD.state <> 'closed' OR NEW.state <> 'closed'
                       OR OLD.opened_at IS NOT NULL OR NEW.opened_at IS NOT NULL
                       OR NOT EXISTS (
                            SELECT 1 FROM release_candidates old_candidate
                             WHERE old_candidate.candidate_id = OLD.candidate_id
                               AND old_candidate.generation_id = OLD.generation_id
                               AND old_candidate.status = 'aborted'
                       )
                       OR NOT EXISTS (
                            SELECT 1 FROM release_candidates new_candidate
                             WHERE new_candidate.candidate_id = NEW.candidate_id
                               AND new_candidate.generation_id = NEW.generation_id
                               AND new_candidate.status = 'assembling'
                       )
                       OR EXISTS (
                            SELECT 1 FROM authority_activations activation
                             WHERE activation.generation_id = OLD.generation_id
                               AND activation.outcome = 'activated'
                       ) THEN
                        RAISE EXCEPTION 'mutation admission candidate may rebind only after exact pre-burn abort';
                    END IF;
                    RETURN NEW;
                END IF;
                IF OLD.state <> 'closed' OR NEW.state <> 'open' THEN
                    RAISE EXCEPTION 'mutation admission may open exactly once';
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
        _install_postgresql_admission_function(isolated_first_request=False)
    elif bind.dialect.name == "sqlite":
        for trigger_name in (
            "service_requests_stage6_admission_guard",
            "mutation_admission_controls_verified_open_guard",
            "release_candidates_initial_state_guard",
            "mutation_admission_controls_initial_state_guard",
            "first_request_reservations_initial_state_guard",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    _drop_generation_constraints()
