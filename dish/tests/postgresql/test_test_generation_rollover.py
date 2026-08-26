from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from dish_pg import models
from dish_pg import reservation_models as reservations
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.repositories import DishRepository, RegistryRepository, ScalarMutationSource
from dish_pg.test_generation_rollover import (
    CREATION_REASON,
    TEST_DATABASE_NAME,
    GenerationRolloverError as RolloverError,
    _parser,
    _rollover_generation_transaction,
    require_test_database_url,
    rollover_test_generation,
)
from dish_pg.workflow import WorkflowAuthorityService
from tests.support.postgresql import first_admission as first_admission_support
from tests.support.postgresql.first_admission import (
    _activate_authority,
    _burn_and_open_admission,
    _prepare_approved_cutover,
)
from tests.support.postgresql.workflow import NOW, _admit, _next, _register_run

pytestmark = pytest.mark.database_boundary
SOURCE_COMMIT = "f" * 40
UNKNOWN_CANDIDATE_ID = uuid.UUID(int=997)
UNKNOWN_CUTOVER_ID = uuid.UUID(int=998)
UNKNOWN_RESERVATION_ID = uuid.UUID(int=999)


def _install_verification_queue(factory, ids, context) -> uuid.UUID:
    with session_scope(factory) as session:
        section_id = _next(ids)
        session.add(
            models.GovernedSection(
                section_id=section_id,
                project_id=context["project_id"],
                logical_name="Verification Queue",
                lifecycle="active",
                import_run_id=context["import_run_id"],
                created_at=NOW,
                retired_at=None,
            )
        )
        session.flush()
        session.add(
            models.SectionExternalAlias(
                alias_id=_next(ids),
                section_id=section_id,
                external_system="asana",
                external_id="1217084805070732",
                origin="imported",
                import_run_id=context["import_run_id"],
                projection_event_id=None,
                state="active",
                created_at=NOW,
                retired_at=None,
            )
        )
        current = session.get(models.ActiveSectionRegistry, context["generation_id"])
        source = session.get(models.SectionRegistryVersion, current.registry_version_id)
        version_id = _next(ids)
        activation_id = _next(ids)
        RegistryRepository(session).add_registry_version(
            models.SectionRegistryVersion(
                registry_version_id=version_id,
                generation_id=context["generation_id"],
                version_number=2,
                import_run_id=source.import_run_id,
                contract_binding_id=source.contract_binding_id,
                registry_sha256="d" * 64,
                created_at=NOW,
            ),
            [
                models.SectionRegistryEntry(
                    registry_version_id=version_id,
                    section_id=context["section_id"],
                    ordinal=0,
                    display_name="Research Queue",
                    workflow_role="imported-section-1217084805070731",
                ),
                models.SectionRegistryEntry(
                    registry_version_id=version_id,
                    section_id=section_id,
                    ordinal=1,
                    display_name="Verification Queue",
                    workflow_role="verification_queue",
                ),
            ],
        )
        session.add(
            models.SectionRegistryActivation(
                registry_activation_id=activation_id,
                generation_id=context["generation_id"],
                registry_version_id=version_id,
                activation_route="import",
                import_run_id=context["import_run_id"],
                command_execution_id=None,
                registry_revision=2,
                activated_at=NOW,
            )
        )
        session.flush()
        states = session.scalars(
            select(models.DishState)
            .where(models.DishState.generation_id == context["generation_id"])
            .order_by(models.DishState.task_id)
        ).all()
        for state in states:
            head = session.get(
                models.TaskMembershipHead,
                (context["generation_id"], state.task_id),
            )
            assert head is not None
            mutation = DishRepository(
                session, uuid_factory=lambda: _next(ids)
            ).begin_scalar_mutation(
                generation_id=context["generation_id"],
                task_id=state.task_id,
                expected_dish_version=state.dish_version,
                expected_membership_revision=head.membership_revision,
                source=ScalarMutationSource(
                    route="import",
                    import_run_id=context["import_run_id"],
                    occurred_at=NOW,
                ),
            )
            mutation.place(
                section_id=state.section_id,
                registry_version_id=version_id,
            )
            mutation.finalize()
        current.registry_version_id = version_id
        current.registry_activation_id = activation_id
        current.registry_revision = 2
        current.updated_at = NOW
    return section_id


def _contaminate(factory, ids, context, task_id):
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    _burn_and_open_admission(factory, ids, context, task_id, candidate_id, cutover_id)
    with session_scope(factory) as session:
        reservation_id = session.scalar(
            select(reservations.FirstRequestReservation.reservation_id).where(
                reservations.FirstRequestReservation.generation_id == context["generation_id"]
            )
        )
        assert reservation_id is not None
    return candidate_id, cutover_id, reservation_id


def _row_payload(row) -> dict[str, object]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _stage6_forensic_payload(factory, candidate_id, cutover_id, generation_id):
    with session_scope(factory) as session:
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        cutover = session.get(rel.CutoverRun, cutover_id)
        control = session.get(rel.MutationAdmissionControl, generation_id)
        reservation = session.scalar(
            select(reservations.FirstRequestReservation).where(
                reservations.FirstRequestReservation.generation_id == generation_id
            )
        )
        baseline = session.get(tx.ShadowBaseline, candidate.shadow_baseline_id)
        epoch = session.get(tx.ProjectionEpoch, candidate.projection_epoch_id)
        return {
            "candidate": _row_payload(candidate),
            "cutover": _row_payload(cutover),
            "control": _row_payload(control),
            "reservation": _row_payload(reservation),
            "baseline": _row_payload(baseline),
            "epoch": _row_payload(epoch),
        }


def test_rollover_preserves_contaminated_generation_and_new_admission_isolated(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, cutover_id, reservation_id = _contaminate(factory, ids, context, task_id)
    verification_section_id = _install_verification_queue(factory, ids, context)
    before = _stage6_forensic_payload(
        factory, candidate_id, cutover_id, context["generation_id"]
    )

    with session_scope(factory) as session:
        alias_count_before = int(session.scalar(select(func.count()).select_from(models.TaskExternalAlias)))
        result = _rollover_generation_transaction(
            session,
            predecessor_generation_id=context["generation_id"],
            contaminated_candidate_id=candidate_id,
            contaminated_cutover_run_id=cutover_id,
            contaminated_reservation_id=reservation_id,
            research_queue_section_id=context["section_id"],
            verification_queue_section_id=verification_section_id,
            source_commit=SOURCE_COMMIT,
            uuid_factory=lambda: _next(ids),
            clock=lambda: NOW + timedelta(hours=1),
        )
        assert result.contamination.candidate_id == candidate_id
        assert result.contamination.cutover_run_id == cutover_id
        assert result.task_count == 1
        assert session.get(models.AuthorityGeneration, context["generation_id"]).status == "retired"
        successor = session.get(models.AuthorityGeneration, result.generation_id)
        assert successor.status == "active"
        assert successor.creation_reason == CREATION_REASON
        assert successor.predecessor_generation_id == context["generation_id"]
        assert successor.external_restore_control_id is None
        assert session.scalar(
            select(rel.ReleaseCandidate.candidate_id).where(
                rel.ReleaseCandidate.generation_id == result.generation_id
            )
        ) is None
        assert session.get(rel.MutationAdmissionControl, result.generation_id) is None
        assert session.scalar(
            select(reservations.FirstRequestReservation.reservation_id).where(
                reservations.FirstRequestReservation.generation_id == result.generation_id
            )
        ) is None
        assert int(session.scalar(select(func.count()).select_from(models.TaskExternalAlias))) == alias_count_before

        roles = {
            row.workflow_role: row.section_id
            for row in session.scalars(
                select(models.SectionRegistryEntry).where(
                    models.SectionRegistryEntry.registry_version_id == result.registry_version_id
                )
            )
        }
        assert roles["research_queue"] == context["section_id"]
        assert roles["verification_queue"] == verification_section_id
        new_epoch = session.get(tx.ProjectionEpoch, result.projection_epoch_id)
        assert new_epoch.external_effects_enabled is False
        assert session.get(tx.ShadowBaseline, result.shadow_baseline_id).status == "open"

        run_id = _next(ids)
        _register_run(session, generation_id=result.generation_id, run_id=run_id)
        admitted = _admit(
            WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids)),
            request_id=_next(ids),
            generation_id=result.generation_id,
            run_id=run_id,
            payload={"task_id": str(task_id)},
        )
        assert admitted.replayed is False

    after = _stage6_forensic_payload(
        factory, candidate_id, cutover_id, context["generation_id"]
    )
    assert after == before


def test_rollover_refuses_same_shape_first_admission_without_incident_signature(
    workflow_db, monkeypatch
) -> None:
    factory, ids, context, task_id = workflow_db
    original_prepare_candidate = first_admission_support._prepare_candidate

    def _prepare_nonincident_candidate(*args, **kwargs):
        kwargs["source_manifest_sha256"] = "b" * 64
        return original_prepare_candidate(*args, **kwargs)

    monkeypatch.setattr(
        first_admission_support, "_prepare_candidate", _prepare_nonincident_candidate
    )
    candidate_id, cutover_id, reservation_id = _contaminate(
        factory, ids, context, task_id
    )
    verification_section_id = _install_verification_queue(factory, ids, context)
    before = _stage6_forensic_payload(
        factory, candidate_id, cutover_id, context["generation_id"]
    )

    with session_scope(factory) as session:
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        cutover = session.get(rel.CutoverRun, cutover_id)
        reservation = session.get(reservations.FirstRequestReservation, reservation_id)
        assert candidate.status == "activated"
        assert cutover.state == "admission_open"
        assert cutover.rehearsal_id is None
        assert reservation.state == "reserved"
        with pytest.raises(RolloverError, match="incident signature"):
            _rollover_generation_transaction(
                session,
                predecessor_generation_id=context["generation_id"],
                contaminated_candidate_id=candidate_id,
                contaminated_cutover_run_id=cutover_id,
                contaminated_reservation_id=reservation_id,
                research_queue_section_id=context["section_id"],
                verification_queue_section_id=verification_section_id,
                source_commit=SOURCE_COMMIT,
                uuid_factory=lambda: _next(ids),
                clock=lambda: NOW + timedelta(hours=1),
            )
        predecessor = session.get(models.AuthorityGeneration, context["generation_id"])
        assert predecessor.status == "active"
        assert predecessor.retired_at is None
        assert int(
            session.scalar(
                select(func.count())
                .select_from(models.AuthorityGeneration)
                .where(models.AuthorityGeneration.creation_reason == CREATION_REASON)
            )
        ) == 0

    assert _stage6_forensic_payload(
        factory, candidate_id, cutover_id, context["generation_id"]
    ) == before


def test_rollover_refuses_wrong_explicit_incident_identity(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, cutover_id, reservation_id = _contaminate(
        factory, ids, context, task_id
    )
    verification_section_id = _install_verification_queue(factory, ids, context)
    before = _stage6_forensic_payload(
        factory, candidate_id, cutover_id, context["generation_id"]
    )
    with pytest.raises(RolloverError, match="explicit contaminated"):
        with session_scope(factory) as session:
            _rollover_generation_transaction(
                session,
                predecessor_generation_id=context["generation_id"],
                contaminated_candidate_id=UNKNOWN_CANDIDATE_ID,
                contaminated_cutover_run_id=cutover_id,
                contaminated_reservation_id=reservation_id,
                research_queue_section_id=context["section_id"],
                verification_queue_section_id=verification_section_id,
                source_commit=SOURCE_COMMIT,
                uuid_factory=lambda: _next(ids),
                clock=lambda: NOW + timedelta(hours=1),
            )
    with session_scope(factory) as session:
        predecessor = session.get(models.AuthorityGeneration, context["generation_id"])
        assert predecessor.status == "active"
        assert predecessor.retired_at is None
    assert _stage6_forensic_payload(
        factory, candidate_id, cutover_id, context["generation_id"]
    ) == before


def test_rollover_refuses_clean_predecessor(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    verification_section_id = _install_verification_queue(factory, ids, context)
    with pytest.raises(RolloverError, match="explicit contaminated"):
        with session_scope(factory) as session:
            _rollover_generation_transaction(
                session,
                predecessor_generation_id=context["generation_id"],
                contaminated_candidate_id=UNKNOWN_CANDIDATE_ID,
                contaminated_cutover_run_id=UNKNOWN_CUTOVER_ID,
                contaminated_reservation_id=UNKNOWN_RESERVATION_ID,
                research_queue_section_id=context["section_id"],
                verification_queue_section_id=verification_section_id,
                source_commit=SOURCE_COMMIT,
                uuid_factory=lambda: _next(ids),
                clock=lambda: NOW,
            )


def test_rollover_refuses_non_test_database_before_mutation(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        before = session.get(models.AuthorityGeneration, context["generation_id"]).status
        with pytest.raises(RolloverError, match="PostgreSQL session"):
            rollover_test_generation(
                session,
                predecessor_generation_id=context["generation_id"],
                contaminated_candidate_id=UNKNOWN_CANDIDATE_ID,
                contaminated_cutover_run_id=UNKNOWN_CUTOVER_ID,
                contaminated_reservation_id=UNKNOWN_RESERVATION_ID,
                research_queue_section_id=context["section_id"],
                verification_queue_section_id=uuid.uuid4(),
                source_commit=SOURCE_COMMIT,
                uuid_factory=lambda: _next(ids),
                clock=lambda: NOW,
            )
        assert session.get(models.AuthorityGeneration, context["generation_id"]).status == before


def test_rollover_refuses_non_active_predecessor(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        predecessor = session.get(models.AuthorityGeneration, context["generation_id"])
        predecessor.status = "retired"
        predecessor.retired_at = NOW
    with pytest.raises(RolloverError, match="must be status='active'"):
        with session_scope(factory) as session:
            _rollover_generation_transaction(
                session,
                predecessor_generation_id=context["generation_id"],
                contaminated_candidate_id=UNKNOWN_CANDIDATE_ID,
                contaminated_cutover_run_id=UNKNOWN_CUTOVER_ID,
                contaminated_reservation_id=UNKNOWN_RESERVATION_ID,
                research_queue_section_id=context["section_id"],
                verification_queue_section_id=uuid.uuid4(),
                source_commit=SOURCE_COMMIT,
                uuid_factory=lambda: _next(ids),
                clock=lambda: NOW,
            )


def test_rollover_is_atomic_if_failure_occurs_after_generation_swap(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, cutover_id, reservation_id = _contaminate(factory, ids, context, task_id)
    verification_section_id = _install_verification_queue(factory, ids, context)
    before = _stage6_forensic_payload(
        factory, candidate_id, cutover_id, context["generation_id"]
    )

    with pytest.raises(RuntimeError, match="injected rollover failure"):
        with session_scope(factory) as session:
            _rollover_generation_transaction(
                session,
                predecessor_generation_id=context["generation_id"],
                contaminated_candidate_id=candidate_id,
                contaminated_cutover_run_id=cutover_id,
                contaminated_reservation_id=reservation_id,
                research_queue_section_id=context["section_id"],
                verification_queue_section_id=verification_section_id,
                source_commit=SOURCE_COMMIT,
                uuid_factory=lambda: _next(ids),
                clock=lambda: NOW + timedelta(hours=1),
                failure_hook=lambda: (_ for _ in ()).throw(RuntimeError("injected rollover failure")),
            )

    with session_scope(factory) as session:
        predecessor = session.get(models.AuthorityGeneration, context["generation_id"])
        assert predecessor.status == "active"
        assert predecessor.retired_at is None
        assert int(
            session.scalar(
                select(func.count())
                .select_from(models.AuthorityGeneration)
                .where(models.AuthorityGeneration.creation_reason == CREATION_REASON)
            )
        ) == 0
    assert _stage6_forensic_payload(
        factory, candidate_id, cutover_id, context["generation_id"]
    ) == before


def test_rollover_cli_requires_explicit_incident_ids() -> None:
    parser = _parser()
    option_strings = {
        option
        for action in parser._actions
        for option in action.option_strings
    }
    assert {
        "--contaminated-candidate-id",
        "--contaminated-cutover-run-id",
        "--contaminated-reservation-id",
    } <= option_strings



def test_database_url_guard_requires_explicit_exact_test_target() -> None:
    with pytest.raises(RolloverError, match="must be set explicitly"):
        require_test_database_url("")
    with pytest.raises(RolloverError, match="containing 'prod'"):
        require_test_database_url("postgresql+psycopg://dish@localhost/dish_stage_a_prod")
    with pytest.raises(RolloverError, match="exact TEST database"):
        require_test_database_url("postgresql+psycopg://dish@localhost/dish_stage_a_dev")
    require_test_database_url(
        "postgresql+psycopg://dish@localhost/dish_stage_a_test"
    )


def test_migration_allows_honest_fixture_recovery_generation(sqlite_migration_database) -> None:
    predecessor = "00000000000000000000000000000001"
    successor = "00000000000000000000000000000002"
    sqlite_migration_database.initialize("0040_no_asana_post_burn")

    def seed(connection):
        connection.execute(
            text(
                "INSERT INTO authority_generations("
                "generation_id,predecessor_generation_id,creation_reason,external_restore_control_id,"
                "schema_head,dish_release,status,created_at,retired_at"
                ") VALUES (:generation_id,NULL,'initial_cutover',NULL,:schema_head,:dish_release,'active',:created_at,NULL)"
            ),
            {
                "generation_id": predecessor,
                "schema_head": "0040_no_asana_post_burn",
                "dish_release": "dish-test",
                "created_at": NOW,
            },
        )

    sqlite_migration_database.seed(seed)
    sqlite_migration_database.upgrade("0041_test_generation_rollover")

    def insert_successor(connection):
        connection.execute(
            text(
                "UPDATE authority_generations SET status='retired', retired_at=:at "
                "WHERE generation_id=:generation_id"
            ),
            {"at": NOW, "generation_id": predecessor},
        )
        connection.execute(
            text(
                "INSERT INTO authority_generations("
                "generation_id,predecessor_generation_id,creation_reason,external_restore_control_id,"
                "schema_head,dish_release,status,created_at,retired_at"
                ") VALUES (:generation_id,:predecessor,'test_fixture_recovery',NULL,"
                ":schema_head,:dish_release,'active',:created_at,NULL)"
            ),
            {
                "generation_id": successor,
                "predecessor": predecessor,
                "schema_head": "0041_test_generation_rollover",
                "dish_release": "dish-test",
                "created_at": NOW,
            },
        )

    sqlite_migration_database.seed(insert_successor)
    assert sqlite_migration_database.read(
        lambda connection: connection.execute(
            text(
                "SELECT creation_reason FROM authority_generations "
                "WHERE generation_id=:generation_id"
            ),
            {"generation_id": successor},
        ).scalar_one()
    ) == "test_fixture_recovery"
