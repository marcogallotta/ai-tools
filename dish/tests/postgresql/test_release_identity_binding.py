from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import models
from dish_pg import stage6_models as rel
from dish_pg.candidate_manifest import build_candidate_manifest
from dish_pg.command_port import PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.release import ReleaseAuthorityError, ReleaseCandidateService
from dish_pg.repositories import DishRepository, RegistryRepository, ScalarMutationSource
from tests.support.postgresql.release import HASH_A, HASH_B, _prepare_candidate, _record_final_closure
from tests.support.postgresql.release_oracles import EXPECTED_EVIDENCE_ARTIFACT_KINDS
from tests.support.postgresql.stage8_cutover_evidence_gates import (
    _prepare_fenced_recertified_cutover,
)
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _add_release_binding(
    session,
    ids,
    *,
    generation_id,
    dish_release: str,
    honest_release: str = "honest-identity-2",
    protocol_release: str = "protocol-identity-2",
    resolved_at=None,
):
    binding = models.HonestContractBinding(
        binding_id=_next(ids),
        binding_kind="release",
        source_identity=f"honest-pantry@{honest_release}",
        dish_release=dish_release,
        honest_release=honest_release,
        protocol_release=protocol_release,
        protocol_sha256="c" * 64,
        schema_release="schema-identity-2",
        schema_sha256="d" * 64,
        migration_id=None,
        source_schema_version=None,
        target_schema_version=None,
        migration_metadata_sha256=None,
        source_ids={"repo": "identity-regression"},
        provenance={"resolved_by": "identity-regression"},
        resolved_at=resolved_at or NOW + timedelta(minutes=10),
    )
    session.add(binding)
    session.flush()
    return binding


def _move_active_registry_to_binding(session, ids, context, *, binding, activated_at):
    current = session.get(models.ActiveSectionRegistry, context["generation_id"])
    assert current is not None
    previous = session.get(models.SectionRegistryVersion, current.registry_version_id)
    assert previous is not None
    entries = session.scalars(
        select(models.SectionRegistryEntry).where(
            models.SectionRegistryEntry.registry_version_id == previous.registry_version_id
        )
    ).all()
    registry = models.SectionRegistryVersion(
        registry_version_id=_next(ids),
        generation_id=context["generation_id"],
        version_number=previous.version_number + 1,
        import_run_id=previous.import_run_id,
        contract_binding_id=binding.binding_id,
        registry_sha256="e" * 64,
        created_at=activated_at,
    )
    repo = RegistryRepository(session)
    repo.add_registry_version(
        registry,
        [
            models.SectionRegistryEntry(
                registry_version_id=registry.registry_version_id,
                section_id=entry.section_id,
                ordinal=entry.ordinal,
                display_name=entry.display_name,
                workflow_role=entry.workflow_role,
            )
            for entry in entries
        ],
    )
    dishes = DishRepository(session, uuid_factory=lambda: _next(ids))
    states = session.scalars(
        select(models.DishState)
        .where(models.DishState.generation_id == context["generation_id"])
        .order_by(models.DishState.task_id)
    ).all()
    for state in states:
        membership = session.get(
            models.TaskMembershipHead,
            (context["generation_id"], state.task_id),
        )
        assert membership is not None
        mutation = dishes.begin_scalar_mutation(
            generation_id=context["generation_id"],
            task_id=state.task_id,
            expected_dish_version=state.dish_version,
            expected_membership_revision=membership.membership_revision,
            source=ScalarMutationSource(
                route="import",
                import_run_id=registry.import_run_id,
                occurred_at=activated_at,
            ),
        )
        mutation.place(
            section_id=state.section_id,
            registry_version_id=registry.registry_version_id,
        )
        mutation.finalize()
    activation = models.SectionRegistryActivation(
        registry_activation_id=_next(ids),
        generation_id=context["generation_id"],
        registry_version_id=registry.registry_version_id,
        activation_route="import",
        import_run_id=registry.import_run_id,
        command_execution_id=None,
        registry_revision=current.registry_revision + 1,
        activated_at=activated_at,
    )
    repo.activate_registry(
        activation=activation,
        current=models.ActiveSectionRegistry(
            generation_id=context["generation_id"],
            registry_version_id=registry.registry_version_id,
            registry_activation_id=activation.registry_activation_id,
            registry_revision=activation.registry_revision,
            updated_at=activated_at,
        ),
    )
    return registry


def test_candidate_b_rejects_source_a_evidence_and_rehearsal(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_environment = f"production-shaped@{HASH_B}"
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(
            session,
            ids,
            context,
            task_id,
            source_manifest_sha256=HASH_B,
            rehearsal_environment_identity=candidate_environment,
        )
        with pytest.raises(ReleaseAuthorityError, match="source manifest.*candidate"):
            service.record_evidence(
                candidate_id=candidate_id,
                category="authority_coverage",
                evidence_key="current_to_target",
                outcome="pass",
                payload={
                    "artifact_kind": EXPECTED_EVIDENCE_ARTIFACT_KINDS[
                        ("authority_coverage", "current_to_target")
                    ],
                    "artifact_identity": "foreign-source-a",
                    "artifact_path": "/not-observed-because-source-mismatch",
                    "artifact_sha256": HASH_A,
                    "source_manifest_sha256": HASH_A,
                    "gate_name": "authority_coverage:current_to_target",
                    "gate_result": "pass",
                },
                recorded_at=NOW,
            )
        with pytest.raises(ReleaseAuthorityError, match="source manifest.*candidate"):
            service.start_rehearsal(
                candidate_id=candidate_id,
                rehearsal_kind="restore",
                environment_identity=candidate_environment,
                source_manifest_sha256=HASH_A,
                started_at=NOW,
            )


def test_wrong_governed_rehearsal_environment_is_rejected(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_environment = f"production-shaped@{HASH_B}"
    wrong_but_governed_environment = f"native-postgresql@{HASH_B}"
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(
            session,
            ids,
            context,
            task_id,
            source_manifest_sha256=HASH_B,
            rehearsal_environment_identity=candidate_environment,
        )
        with pytest.raises(ReleaseAuthorityError, match="environment identity.*candidate"):
            service.start_rehearsal(
                candidate_id=candidate_id,
                rehearsal_kind="restore",
                environment_identity=wrong_but_governed_environment,
                source_manifest_sha256=HASH_B,
                started_at=NOW,
            )


def test_candidate_approval_rejects_active_registry_drift(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
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
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        replacement = _add_release_binding(
            session,
            ids,
            generation_id=generation.generation_id,
            dish_release=generation.dish_release,
        )
        _move_active_registry_to_binding(
            session,
            ids,
            context,
            binding=replacement,
            activated_at=NOW + timedelta(minutes=4, seconds=30),
        )

        with pytest.raises(
            ReleaseAuthorityError,
            match="active generation.*registry.*Honest",
        ):
            service.approve_candidate(
                candidate_id=candidate_id,
                evidence_bundle_id=bundle.bundle_id,
                approver="Marco",
                approval_statement="Must remain bound to the validated release identity.",
                approval_payload={
                    "final_asana_closure_id": str(closure.closure_id),
                    "final_asana_closure_sha256": closure.closure_sha256,
                },
                approved_at=NOW + timedelta(minutes=5),
            )
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        assert candidate is not None
        assert candidate.status == "validated"
        assert session.scalar(
            select(manifest_models.ReleaseCandidateManifest).where(
                manifest_models.ReleaseCandidateManifest.candidate_id == candidate_id
            )
        ) is None


def test_candidate_manifest_rejects_active_registry_drift(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert candidate is not None
        assert generation is not None
        candidate_registry_version_id = candidate.registry_version_id
        candidate_honest_binding_id = candidate.honest_binding_id

        replacement = _add_release_binding(
            session,
            ids,
            generation_id=generation.generation_id,
            dish_release=generation.dish_release,
        )
        replacement_registry = _move_active_registry_to_binding(
            session,
            ids,
            context,
            binding=replacement,
            activated_at=NOW + timedelta(seconds=30),
        )
        assert replacement_registry.registry_version_id != candidate_registry_version_id
        assert replacement.binding_id != candidate_honest_binding_id

        with pytest.raises(
            ReleaseAuthorityError,
            match="candidate manifest active registry does not match candidate release identity",
        ):
            build_candidate_manifest(
                session,
                uuid_factory=lambda: _next(ids),
                candidate=candidate,
                built_at=NOW + timedelta(minutes=1),
            )


def test_active_release_contract_drift_blocks_candidate_validation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW,
        )
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        replacement = _add_release_binding(
            session,
            ids,
            generation_id=generation.generation_id,
            dish_release=generation.dish_release,
        )
        _move_active_registry_to_binding(
            session,
            ids,
            context,
            binding=replacement,
            activated_at=NOW + timedelta(seconds=30),
        )

        with pytest.raises(ReleaseAuthorityError, match="active generation.*registry.*Honest"):
            service.validate_candidate(
                candidate_id=candidate_id,
                evidence_bundle_id=bundle.bundle_id,
                validated_at=NOW + timedelta(minutes=1),
            )


def test_generation_release_mismatch_blocks_candidate_validation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW,
        )
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        generation.dish_release = "dish-release-drifted-after-candidate"
        session.flush()

        with pytest.raises(ReleaseAuthorityError, match="exact Honest release binding"):
            service.validate_candidate(
                candidate_id=candidate_id,
                evidence_bundle_id=bundle.bundle_id,
                validated_at=NOW + timedelta(minutes=1),
            )


def test_active_honest_protocol_drift_blocks_rollback_burn(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, closure, run, fence = _prepare_fenced_recertified_cutover(
            session, ids, context, task_id
        )
        service.activate_authority(
            cutover_run_id=run.cutover_run_id,
            final_asana_closure_id=closure.closure_id,
            activated_at=NOW + timedelta(minutes=5),
            required_writer_inventory={fence.target_identity},
        )
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        replacement = _add_release_binding(
            session,
            ids,
            generation_id=generation.generation_id,
            dish_release=generation.dish_release,
            honest_release="honest-after-approval",
            protocol_release="protocol-after-approval",
            resolved_at=NOW + timedelta(minutes=5, seconds=20),
        )
        _move_active_registry_to_binding(
            session,
            ids,
            context,
            binding=replacement,
            activated_at=NOW + timedelta(minutes=5, seconds=20),
        )

        with pytest.raises(ReleaseAuthorityError, match="active generation.*registry.*Honest"):
            service.burn_rollback(
                cutover_run_id=run.cutover_run_id,
                legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
                burned_at=NOW + timedelta(minutes=6),
                required_writer_inventory={fence.target_identity},
            )
        assert session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"],
                models.AuthorityActivation.outcome == "activated",
            )
        ) is None


def test_newer_same_release_honest_binding_does_not_change_runtime_resolution(
    workflow_db,
) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        port = PostgresCommandPort(session, cursor_secret=b"release-identity-test-secret-32-bytes")
        assert port._binding_for(generation).binding_id == context["binding_id"]

        newer = _add_release_binding(
            session,
            ids,
            generation_id=generation.generation_id,
            dish_release=generation.dish_release,
            honest_release="honest-newer-same-release",
            protocol_release="protocol-newer-same-release",
            resolved_at=NOW + timedelta(days=1),
        )
        assert newer.binding_id != context["binding_id"]
        assert port._binding_for(generation).binding_id == context["binding_id"]


def test_correctly_bound_candidate_evidence_path_still_validates(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        assert candidate is not None
        active = RegistryRepository(session).active_release_contract(candidate.generation_id)
        assert candidate.registry_version_id == active.registry_version.registry_version_id
        assert candidate.honest_binding_id == active.honest_binding.binding_id
        assert candidate.schema_head == active.generation.schema_head
        assert candidate.dish_release == active.generation.dish_release
        assert candidate.honest_release == active.honest_binding.honest_release
        assert candidate.protocol_release == active.honest_binding.protocol_release

        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW,
        )
        evaluation = service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )
        assert evaluation.passed is True
        assert next(
            check for check in evaluation.checks if check.code == "candidate_release_identity_exact"
        ).passed is True
        assert service.candidate_status(candidate_id).status == "validated"
