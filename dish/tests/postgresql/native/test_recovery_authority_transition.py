"""Native PostgreSQL certification for exact release/cutover authority boundaries."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import psycopg
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import final_asana_closure as final_asana_closure_module
from dish_pg import models
from dish_pg import stage6_models as release_models
from dish_pg.database import session_scope
from dish_pg.release import ALEMBIC_HEAD, ReleaseCandidateService
from dish_pg.repositories import RegistryRepository
from tests.support.postgresql.core import (
    _bootstrap_registry,
    _import_one,
    _next,
    core_db,
)
from tests.support.postgresql.release import HASH_A, _prepare_candidate, _record_final_closure
from tests.support.postgresql.stage8_cutover_evidence_gates import (
    _prepare_fenced_recertified_cutover,
)
from tests.support.postgresql.workflow import NOW
from tests.support.postgresql.concurrency import (
    TransactionGate,
    assert_transaction_aborted,
    assert_transaction_committed,
    execute_transaction,
    independent_connections,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _stage_replacement_registry(session, ids, context, *, suffix: str, activated_at):
    active = RegistryRepository(session).active_release_contract(context["generation_id"])
    replacement_binding = models.HonestContractBinding(
        binding_id=_next(ids),
        binding_kind="release",
        source_identity=f"honest-pantry@native-{suffix}",
        dish_release=active.generation.dish_release,
        honest_release=f"honest-native-{suffix}",
        protocol_release=f"protocol-native-{suffix}",
        protocol_sha256="c" * 64,
        schema_release=f"schema-native-{suffix}",
        schema_sha256="d" * 64,
        migration_id=None,
        source_schema_version=None,
        target_schema_version=None,
        migration_metadata_sha256=None,
        source_ids={"repo": "native-release-identity"},
        provenance={"resolved_by": "native-release-identity"},
        resolved_at=activated_at,
    )
    session.add(replacement_binding)
    session.flush()
    replacement_version = models.SectionRegistryVersion(
        registry_version_id=_next(ids),
        generation_id=context["generation_id"],
        version_number=active.registry_version.version_number + 1,
        import_run_id=active.registry_version.import_run_id,
        contract_binding_id=replacement_binding.binding_id,
        registry_sha256="e" * 64,
        created_at=activated_at,
    )
    entries = session.scalars(
        select(models.SectionRegistryEntry).where(
            models.SectionRegistryEntry.registry_version_id
            == active.registry_version.registry_version_id
        )
    ).all()
    RegistryRepository(session).add_registry_version(
        replacement_version,
        [
            models.SectionRegistryEntry(
                registry_version_id=replacement_version.registry_version_id,
                section_id=entry.section_id,
                ordinal=entry.ordinal,
                display_name=entry.display_name,
                workflow_role=entry.workflow_role,
            )
            for entry in entries
        ],
    )
    replacement_activation = models.SectionRegistryActivation(
        registry_activation_id=_next(ids),
        generation_id=context["generation_id"],
        registry_version_id=replacement_version.registry_version_id,
        activation_route="import",
        import_run_id=replacement_version.import_run_id,
        command_execution_id=None,
        registry_revision=active.active_registry.registry_revision + 1,
        activated_at=activated_at,
    )
    session.add(replacement_activation)
    session.flush()
    return {
        "approved_registry_version_id": active.registry_version.registry_version_id,
        "approved_honest_binding_id": active.honest_binding.binding_id,
        "registry_version_id": replacement_version.registry_version_id,
        "honest_binding_id": replacement_binding.binding_id,
        "registry_activation_id": replacement_activation.registry_activation_id,
        "registry_revision": replacement_activation.registry_revision,
        "updated_at": activated_at,
    }


def _switch_active_registry(session, *, generation_id, replacement) -> None:
    session.execute(
        text(
            """UPDATE active_section_registries
                  SET registry_version_id=:registry_version_id,
                      registry_activation_id=:registry_activation_id,
                      registry_revision=:registry_revision,
                      updated_at=:updated_at
                WHERE generation_id=:generation_id"""
        ),
        {
            "registry_version_id": replacement["registry_version_id"],
            "registry_activation_id": replacement["registry_activation_id"],
            "registry_revision": replacement["registry_revision"],
            "updated_at": replacement["updated_at"],
            "generation_id": generation_id,
        },
    )


def _assert_lock_timeout(outcome) -> None:
    error = assert_transaction_aborted(outcome)
    assert isinstance(error, OperationalError)
    assert isinstance(error.orig, psycopg.errors.LockNotAvailable)


def test_native_candidate_validation_serializes_active_registry_switch(
    core_db, monkeypatch
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        task = _import_one(session, ids, context)
        task_id = task.task_id

    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW,
        )
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        candidate_registry_version_id = candidate.registry_version_id
        candidate_honest_binding_id = candidate.honest_binding_id
        evidence_bundle_id = bundle.bundle_id

    with session_scope(factory) as session:
        replacement = _stage_replacement_registry(
            session,
            ids,
            context,
            suffix="validation-race",
            activated_at=NOW + timedelta(seconds=30),
        )

    boundary = TransactionGate(label="candidate validation after successful evaluation")
    original_evaluate = ReleaseCandidateService.evaluate_candidate

    def gated_evaluate(self, *args, **kwargs):
        evaluation = original_evaluate(self, *args, **kwargs)
        boundary.block()
        return evaluation

    monkeypatch.setattr(ReleaseCandidateService, "evaluate_candidate", gated_evaluate)
    engine = factory.kw["bind"]

    def validate(session):
        return ReleaseCandidateService(
            session, uuid_factory=lambda: _next(ids)
        ).validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=evidence_bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )

    def switch_with_short_lock_timeout(session):
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        _switch_active_registry(
            session,
            generation_id=context["generation_id"],
            replacement=replacement,
        )

    def switch_after_boundary(session):
        _switch_active_registry(
            session,
            generation_id=context["generation_id"],
            replacement=replacement,
        )

    with independent_connections(engine, 2) as (validator_connection, writer_connection):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute_transaction, validator_connection, validate)
            boundary.wait_until_blocked()
            try:
                _assert_lock_timeout(
                    execute_transaction(writer_connection, switch_with_short_lock_timeout)
                )
            finally:
                boundary.release()
            evaluation = assert_transaction_committed(future.result())
            assert evaluation.passed is True
        assert_transaction_committed(
            execute_transaction(writer_connection, switch_after_boundary)
        )

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        active = RegistryRepository(session).active_release_contract(context["generation_id"])
        assert candidate is not None
        assert candidate.status == "validated"
        assert candidate.registry_version_id == candidate_registry_version_id
        assert candidate.honest_binding_id == candidate_honest_binding_id
        assert active.registry_version.registry_version_id == replacement["registry_version_id"]
        assert active.honest_binding.binding_id == replacement["honest_binding_id"]


def test_native_candidate_approval_serializes_manifest_identity(
    core_db, monkeypatch
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        task = _import_one(session, ids, context)
        task_id = task.task_id

    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW,
        )
        service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )
        closure = _record_final_closure(
            service,
            ids,
            candidate_id,
            closed_through_at=NOW + timedelta(minutes=4),
        )
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        candidate_registry_version_id = candidate.registry_version_id
        candidate_honest_binding_id = candidate.honest_binding_id
        evidence_bundle_id = bundle.bundle_id
        closure_id = closure.closure_id
        closure_sha256 = closure.closure_sha256

    with session_scope(factory) as session:
        replacement = _stage_replacement_registry(
            session,
            ids,
            context,
            suffix="approval-race",
            activated_at=NOW + timedelta(minutes=4, seconds=30),
        )

    boundary = TransactionGate(label="candidate approval between evaluation and manifest")
    original_build = final_asana_closure_module.build_candidate_manifest

    def gated_build(*args, **kwargs):
        boundary.block()
        return original_build(*args, **kwargs)

    monkeypatch.setattr(
        final_asana_closure_module,
        "build_candidate_manifest",
        gated_build,
    )
    engine = factory.kw["bind"]

    def approve(session):
        return ReleaseCandidateService(
            session, uuid_factory=lambda: _next(ids)
        ).approve_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=evidence_bundle_id,
            approver="Marco",
            approval_statement="Approve exact R1/H1 candidate under serialized authority.",
            approval_payload={
                "final_asana_closure_id": str(closure_id),
                "final_asana_closure_sha256": closure_sha256,
            },
            approved_at=NOW + timedelta(minutes=5),
        )

    def switch_with_short_lock_timeout(session):
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        _switch_active_registry(
            session,
            generation_id=context["generation_id"],
            replacement=replacement,
        )

    def switch_after_boundary(session):
        _switch_active_registry(
            session,
            generation_id=context["generation_id"],
            replacement=replacement,
        )

    with independent_connections(engine, 2) as (approval_connection, writer_connection):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute_transaction, approval_connection, approve)
            boundary.wait_until_blocked()
            try:
                _assert_lock_timeout(
                    execute_transaction(writer_connection, switch_with_short_lock_timeout)
                )
            finally:
                boundary.release()
            approval = assert_transaction_committed(future.result())
            assert approval.candidate_id == candidate_id
        assert_transaction_committed(
            execute_transaction(writer_connection, switch_after_boundary)
        )

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        manifest = session.scalar(
            select(manifest_models.ReleaseCandidateManifest).where(
                manifest_models.ReleaseCandidateManifest.candidate_id == candidate_id
            )
        )
        active = RegistryRepository(session).active_release_contract(context["generation_id"])
        assert candidate is not None
        assert manifest is not None
        assert candidate.status == "approved"
        assert candidate.registry_version_id == candidate_registry_version_id
        assert candidate.honest_binding_id == candidate_honest_binding_id
        assert manifest.registry_version_id == candidate_registry_version_id
        assert manifest.honest_binding_id == candidate_honest_binding_id
        assert active.registry_version.registry_version_id == replacement["registry_version_id"]
        assert active.honest_binding.binding_id == replacement["honest_binding_id"]


def test_native_direct_validation_transition_locks_active_registry_pointer(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        task = _import_one(session, ids, context)
        task_id = task.task_id

    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW,
        )
        validation_bundle_sha256 = bundle.manifest_sha256

    with session_scope(factory) as session:
        replacement = _stage_replacement_registry(
            session,
            ids,
            context,
            suffix="direct-validation-race",
            activated_at=NOW + timedelta(seconds=30),
        )

    engine = factory.kw["bind"]

    def switch_with_short_lock_timeout(session):
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        _switch_active_registry(
            session,
            generation_id=context["generation_id"],
            replacement=replacement,
        )

    def switch_after_boundary(session):
        _switch_active_registry(
            session,
            generation_id=context["generation_id"],
            replacement=replacement,
        )

    with independent_connections(engine, 2) as (candidate_connection, writer_connection):
        candidate_connection.execute(
            text(
                """UPDATE release_candidates
                      SET status='validated',
                          candidate_revision=candidate_revision + 1,
                          validation_bundle_sha256=:bundle_sha256,
                          validated_at=:validated_at
                    WHERE candidate_id=:candidate_id"""
            ),
            {
                "bundle_sha256": validation_bundle_sha256,
                "validated_at": NOW + timedelta(minutes=1),
                "candidate_id": candidate_id,
            },
        )
        _assert_lock_timeout(
            execute_transaction(writer_connection, switch_with_short_lock_timeout)
        )
        candidate_connection.commit()
        assert_transaction_committed(
            execute_transaction(writer_connection, switch_after_boundary)
        )

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        active = RegistryRepository(session).active_release_contract(context["generation_id"])
        assert candidate is not None
        assert candidate.status == "validated"
        assert candidate.registry_version_id == replacement["approved_registry_version_id"]
        assert candidate.honest_binding_id == replacement["approved_honest_binding_id"]
        assert active.registry_version.registry_version_id == replacement["registry_version_id"]
        assert active.honest_binding.binding_id == replacement["honest_binding_id"]


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
        assert activation.registry_version_id == candidate.registry_version_id
        assert activation.honest_binding_id == candidate.honest_binding_id

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
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="release candidate identity is immutable",
        ):
            raw.execute(
                "UPDATE release_candidates SET source_manifest_sha256=%s WHERE candidate_id=%s",
                ("b" * 64, candidate_id),
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


def test_native_rollback_burn_serializes_exact_active_registry_contract(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        task = _import_one(session, ids, context)
        task_id = task.task_id

    with session_scope(factory) as session:
        _service, candidate_id, closure, run, fence = _prepare_fenced_recertified_cutover(
            session, ids, context, task_id
        )
        cutover_run_id = run.cutover_run_id
        closure_id = closure.closure_id
        writer_identity = fence.target_identity

    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        service.activate_authority(
            cutover_run_id=cutover_run_id,
            final_asana_closure_id=closure_id,
            activated_at=NOW + timedelta(minutes=5),
            required_writer_inventory={writer_identity},
        )

    with session_scope(factory) as session:
        active = RegistryRepository(session).active_release_contract(context["generation_id"])
        replacement_binding = models.HonestContractBinding(
            binding_id=_next(ids),
            binding_kind="release",
            source_identity="honest-pantry@native-same-release-drift",
            dish_release=active.generation.dish_release,
            honest_release="honest-native-drift",
            protocol_release="protocol-native-drift",
            protocol_sha256="c" * 64,
            schema_release="schema-native-drift",
            schema_sha256="d" * 64,
            migration_id=None,
            source_schema_version=None,
            target_schema_version=None,
            migration_metadata_sha256=None,
            source_ids={"repo": "native-release-identity"},
            provenance={"resolved_by": "native-release-identity"},
            resolved_at=NOW + timedelta(minutes=5, seconds=10),
        )
        session.add(replacement_binding)
        session.flush()
        replacement_version = models.SectionRegistryVersion(
            registry_version_id=_next(ids),
            generation_id=context["generation_id"],
            version_number=active.registry_version.version_number + 1,
            import_run_id=active.registry_version.import_run_id,
            contract_binding_id=replacement_binding.binding_id,
            registry_sha256="e" * 64,
            created_at=NOW + timedelta(minutes=5, seconds=10),
        )
        entries = session.query(models.SectionRegistryEntry).filter_by(
            registry_version_id=active.registry_version.registry_version_id
        ).all()
        RegistryRepository(session).add_registry_version(
            replacement_version,
            [
                models.SectionRegistryEntry(
                    registry_version_id=replacement_version.registry_version_id,
                    section_id=entry.section_id,
                    ordinal=entry.ordinal,
                    display_name=entry.display_name,
                    workflow_role=entry.workflow_role,
                )
                for entry in entries
            ],
        )
        replacement_activation = models.SectionRegistryActivation(
            registry_activation_id=_next(ids),
            generation_id=context["generation_id"],
            registry_version_id=replacement_version.registry_version_id,
            activation_route="import",
            import_run_id=replacement_version.import_run_id,
            command_execution_id=None,
            registry_revision=active.active_registry.registry_revision + 1,
            activated_at=NOW + timedelta(minutes=5, seconds=10),
        )
        session.add(replacement_activation)
        session.flush()
        replacement_version_id = replacement_version.registry_version_id
        replacement_activation_id = replacement_activation.registry_activation_id
        replacement_revision = replacement_activation.registry_revision
        approved_registry_version_id = active.registry_version.registry_version_id
        approved_binding_id = active.honest_binding.binding_id

    engine = factory.kw["bind"]

    def competing_registry_switch() -> None:
        raw = engine.raw_connection()
        try:
            raw.driver_connection.autocommit = True
            raw.execute("SET lock_timeout = '250ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                raw.execute(
                    """UPDATE active_section_registries
                          SET registry_version_id=%s, registry_activation_id=%s,
                              registry_revision=%s, updated_at=%s
                        WHERE generation_id=%s""",
                    (
                        replacement_version_id,
                        replacement_activation_id,
                        replacement_revision,
                        NOW + timedelta(minutes=5, seconds=10),
                        context["generation_id"],
                    ),
                )
        finally:
            raw.close()

    with session_scope(factory) as session:
        service = ReleaseCandidateService(
            session,
            uuid_factory=lambda: _next(ids),
            rollback_burn_fence_hook=competing_registry_switch,
        )
        activation = service.burn_rollback(
            cutover_run_id=cutover_run_id,
            legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
            burned_at=NOW + timedelta(minutes=6),
            required_writer_inventory={writer_identity},
        )
        assert activation.registry_version_id == approved_registry_version_id
        assert activation.honest_binding_id == approved_binding_id
        candidate = service._candidate(candidate_id)
        assert activation.registry_version_id == candidate.registry_version_id
        assert activation.honest_binding_id == candidate.honest_binding_id
