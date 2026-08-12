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


def test_claimed_registry_correction_execution_is_not_durable_provenance(tmp_path) -> None:
    from dataclasses import replace
    from datetime import timedelta
    import hashlib
    import json
    import uuid

    from dish_pg.bootstrap import apply_research_queue_role, bootstrap_initial_generation
    from dish_pg.repositories import (
        CoreAuthorityError,
        REGISTRY_ROLE_CORRECTION_KIND,
        REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE,
        RegistryRepository,
        registry_source_import_run,
    )
    from dish_pg.workflow import ExecutionSpec, RequestSpec, WorkflowAuthorityService
    from tests.postgresql import test_initial_bootstrap as bootstrap

    source = bootstrap._source(
        tmp_path,
        bootstrap._record(uuid.uuid4(), "3921"),
        bootstrap._record(
            uuid.uuid4(),
            "3922",
            section_id=bootstrap.OTHER_SECTION_ID,
            section_gid=bootstrap.OTHER_SECTION_GID,
            section_name="Verification Queue",
        ),
    )
    base_spec = bootstrap._spec(source)
    spec = replace(
        base_spec,
        sections=apply_research_queue_role(
            base_spec.sections,
            research_queue_section_id=bootstrap.DEFAULT_SECTION_ID,
        ),
    )
    factory, engine = bootstrap._factory(tmp_path)
    try:
        with session_scope(factory) as session:
            bootstrapped = bootstrap_initial_generation(
                session, spec, clock=lambda: bootstrap.NOW
            )
        with session_scope(factory) as session:
            generation = session.get(
                models.AuthorityGeneration, bootstrapped.generation_id
            )
            predecessor = session.get(
                models.SectionRegistryVersion, bootstrapped.registry_version_id
            )
            assert generation is not None and predecessor is not None

            admin_run, request_id, execution_id = (
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
            )
            arguments = {
                "research_queue_section_id": str(bootstrap.DEFAULT_SECTION_ID),
                "verification_queue_section_id": str(bootstrap.OTHER_SECTION_ID),
            }
            payload = {
                "command": "revise-section-registry",
                "arguments": arguments,
                "owner_id": "Marco",
                "run_id": str(admin_run),
            }
            workflow = WorkflowAuthorityService(session)
            workflow.register_run(
                run_id=admin_run,
                generation_id=bootstrapped.generation_id,
                owner_id="Marco",
                agent="marco",
                capability_digest=b"c" * 32,
                registered_at=bootstrap.NOW,
            )
            admission = workflow.admit_request(
                RequestSpec(
                    request_id=request_id,
                    generation_id=bootstrapped.generation_id,
                    run_id=admin_run,
                    owner_id="Marco",
                    principal_class="admin",
                    command_name="revise-section-registry",
                    canonical_payload=payload,
                    protocol_release=spec.honest.protocol_version,
                    dish_release=generation.dish_release,
                    admitted_at=bootstrap.NOW,
                )
            )
            assert admission.replayed is False
            execution = workflow.begin_execution(
                ExecutionSpec(
                    execution_id=execution_id,
                    request_id=request_id,
                    generation_id=bootstrapped.generation_id,
                    task_id=None,
                    operation_id=None,
                    command_name="revise-section-registry",
                    transaction_profile="L",
                    canonical_intent=payload,
                    pinned_inputs={"now": bootstrap.NOW.isoformat()},
                    contract_binding_id=bootstrapped.binding_id,
                    admitted_at=bootstrap.NOW,
                )
            )
            workflow.repo.claim_execution(
                execution_id=execution_id,
                claimant=f"Marco:{admin_run}",
                claim_token=uuid.uuid4(),
                now=bootstrap.NOW,
                ttl=timedelta(minutes=2),
            )
            session.refresh(execution)
            assert execution.status == "claimed"

            source_import = registry_source_import_run(session, predecessor)
            correction_version_id = uuid.uuid4()
            registry_sha256 = "a" * 64
            requested_roles = {
                "research_queue": str(bootstrap.DEFAULT_SECTION_ID),
                "verification_queue": str(bootstrap.OTHER_SECTION_ID),
            }
            correction_payload = {
                "format": "dish-registry-role-correction-v1",
                "generation_id": str(bootstrapped.generation_id),
                "predecessor_registry_version_id": str(
                    predecessor.registry_version_id
                ),
                "source_import_run_id": str(source_import.import_run_id),
                "command_execution_id": str(execution_id),
                "requested_roles": requested_roles,
                "result_registry_sha256": registry_sha256,
            }
            correction_bundle_sha256 = hashlib.sha256(
                json.dumps(
                    correction_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            correction_import_run_id = uuid.uuid4()
            AuthorityRepository(session).add_import_run(
                models.ImportRun(
                    import_run_id=correction_import_run_id,
                    source_commit=source_import.source_commit,
                    source_release=REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE,
                    legacy_generation_id=source_import.legacy_generation_id,
                    baseline_high_water_mark=(
                        "registry-role-correction:"
                        f"{predecessor.registry_version_id}:{registry_sha256}"
                    ),
                    source_bundle_sha256=correction_bundle_sha256,
                    status="complete",
                    started_at=bootstrap.NOW,
                    completed_at=bootstrap.NOW,
                    provenance={
                        "correction_kind": REGISTRY_ROLE_CORRECTION_KIND,
                        "correction_bundle_sha256": correction_bundle_sha256,
                        "source_import_run_id": str(source_import.import_run_id),
                        "predecessor_registry_version_id": str(
                            predecessor.registry_version_id
                        ),
                        "command_execution_id": str(execution_id),
                        "requested_roles": requested_roles,
                        "result_registry_sha256": registry_sha256,
                        "source_record_count": 0,
                    },
                )
            )
            prior_entries = list(
                session.scalars(
                    select(models.SectionRegistryEntry)
                    .where(
                        models.SectionRegistryEntry.registry_version_id
                        == predecessor.registry_version_id
                    )
                    .order_by(models.SectionRegistryEntry.ordinal)
                )
            )
            correction_version = models.SectionRegistryVersion(
                registry_version_id=correction_version_id,
                generation_id=bootstrapped.generation_id,
                version_number=predecessor.version_number + 1,
                import_run_id=correction_import_run_id,
                contract_binding_id=predecessor.contract_binding_id,
                registry_sha256=registry_sha256,
                created_at=bootstrap.NOW,
            )
            RegistryRepository(session).add_registry_version(
                correction_version,
                [
                    models.SectionRegistryEntry(
                        registry_version_id=correction_version_id,
                        section_id=entry.section_id,
                        ordinal=entry.ordinal,
                        display_name=entry.display_name,
                        workflow_role=(
                            "research_queue"
                            if entry.section_id == bootstrap.DEFAULT_SECTION_ID
                            else "verification_queue"
                            if entry.section_id == bootstrap.OTHER_SECTION_ID
                            else entry.workflow_role
                        ),
                    )
                    for entry in prior_entries
                ],
            )

            session.refresh(execution)
            assert execution.status == "claimed"
            with pytest.raises(
                CoreAuthorityError, match="command execution provenance"
            ):
                registry_source_import_run(session, correction_version)

            for terminal_status in ("failed", "uncertain", "cancelled"):
                execution.status = terminal_status
                execution.claim_owner = None
                execution.claim_token = None
                execution.claim_expires_at = None
                execution.terminal_at = bootstrap.NOW
                execution.execution_revision += 1
                session.flush()
                with pytest.raises(
                    CoreAuthorityError, match="command execution provenance"
                ):
                    registry_source_import_run(session, correction_version)
    finally:
        engine.dispose()
