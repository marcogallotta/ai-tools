from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.bootstrap import bootstrap_initial_generation
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.repositories import (
    AuthorityRepository,
    CoreAuthorityError,
    REGISTRY_ROLE_CORRECTION_KIND,
    REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE,
    RegistryRepository,
    registry_source_import_run,
)
from dish_pg.transition import ProjectionService
from dish_pg.workflow import ExecutionSpec, RequestSpec, WorkflowAuthorityService
from tests.postgresql import test_initial_bootstrap as bootstrap
from tests.support.canonical import TASK
from tests.support.postgresql.core import _next

CERTIFIED_IMPLEMENTATION_SHA = "7e65e1796fb5db6f5545b3acd52a2d60c18d8050"
RESEARCH_GID = "1217084794163035"
VERIFICATION_GID = "1217091890481531"
RESEARCH_ID = uuid.uuid5(uuid.NAMESPACE_URL, f"asana-section:{RESEARCH_GID}")
VERIFICATION_ID = uuid.uuid5(uuid.NAMESPACE_URL, f"asana-section:{VERIFICATION_GID}")
CURSOR_SECRET = b"registry-certification-secret-32b!"


def _active_alias(session, section_id: uuid.UUID) -> models.SectionExternalAlias:
    row = session.scalar(
        select(models.SectionExternalAlias).where(
            models.SectionExternalAlias.section_id == section_id,
            models.SectionExternalAlias.external_system == "asana",
            models.SectionExternalAlias.state == "active",
        )
    )
    assert row is not None
    return row


def _entries(session, registry_version_id: uuid.UUID) -> list[models.SectionRegistryEntry]:
    return list(
        session.scalars(
            select(models.SectionRegistryEntry)
            .where(models.SectionRegistryEntry.registry_version_id == registry_version_id)
            .order_by(models.SectionRegistryEntry.ordinal)
        )
    )


def _call(
    command_name: str,
    *,
    run_id: uuid.UUID,
    request_id: uuid.UUID,
    now,
    protocol_release: str,
    arguments: dict[str, object],
    owner_id: str = "owner-1",
    principal_class: str = "agent",
) -> CommandCall:
    return CommandCall(
        command_name=command_name,
        arguments=arguments,
        owner_id=owner_id,
        principal_class=principal_class,
        run_id=run_id,
        request_id=request_id,
        now=now,
        protocol_release=protocol_release,
    )


def _claimed_correction_probe(
    factory,
    *,
    bootstrapped,
    protocol_release: str,
    research_id: uuid.UUID,
    verification_id: uuid.UUID,
) -> dict[str, str]:
    """Build a structurally valid candidate from a claimed execution, prove it is not durable,
    then prove every non-committed terminal status remains rejected. Roll everything back.
    """

    session = factory()
    transaction = session.begin()
    try:
        predecessor = session.get(
            models.SectionRegistryVersion,
            session.get(models.ActiveSectionRegistry, bootstrapped.generation_id).registry_version_id,
        )
        generation = session.get(models.AuthorityGeneration, bootstrapped.generation_id)
        assert predecessor is not None and generation is not None

        admin_run, request_id, execution_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        arguments = {
            "research_queue_section_id": str(research_id),
            "verification_queue_section_id": str(verification_id),
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
                protocol_release=protocol_release,
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
                contract_binding_id=predecessor.contract_binding_id,
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
        registry_sha256 = hashlib.sha256(
            f"claimed-probe:{correction_version_id}".encode("utf-8")
        ).hexdigest()
        requested_roles = {
            "research_queue": str(research_id),
            "verification_queue": str(verification_id),
        }
        correction_payload = {
            "format": "dish-registry-role-correction-v1",
            "generation_id": str(bootstrapped.generation_id),
            "predecessor_registry_version_id": str(predecessor.registry_version_id),
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
                    "predecessor_registry_version_id": str(predecessor.registry_version_id),
                    "command_execution_id": str(execution_id),
                    "requested_roles": requested_roles,
                    "result_registry_sha256": registry_sha256,
                    "source_record_count": 0,
                },
            )
        )
        prior_entries = _entries(session, predecessor.registry_version_id)
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
                        if entry.section_id == research_id
                        else "verification_queue"
                        if entry.section_id == verification_id
                        else entry.workflow_role
                    ),
                )
                for entry in prior_entries
            ],
        )

        evidence: dict[str, str] = {}
        session.refresh(execution)
        assert execution.status == "claimed"
        with pytest.raises(CoreAuthorityError, match="command execution provenance"):
            registry_source_import_run(session, correction_version)
        evidence["claimed"] = "rejected"

        for terminal_status in ("failed", "uncertain", "cancelled"):
            execution.status = terminal_status
            execution.claim_owner = None
            execution.claim_token = None
            execution.claim_expires_at = None
            execution.terminal_at = bootstrap.NOW
            execution.execution_revision += 1
            session.flush()
            with pytest.raises(CoreAuthorityError, match="command execution provenance"):
                registry_source_import_run(session, correction_version)
            evidence[terminal_status] = "rejected"

        return evidence
    finally:
        transaction.rollback()
        session.close()


@pytest.mark.native_postgresql
def test_registry_correction_handoff_certification(
    core_db,
    tmp_path: Path,
    native_postgresql_identity,
) -> None:
    factory, ids = core_db
    runtime = native_postgresql_identity.as_dict()
    assert runtime["dialect"] == "postgresql"
    assert str(runtime["server_version"]).startswith("17.10")
    assert runtime["database"] == "ai_tools_registry_cert"

    source = bootstrap._source(
        tmp_path,
        bootstrap._record(
            uuid.uuid5(uuid.NAMESPACE_URL, "registry-cert-task:research"),
            "9100000000000001",
            section_id=RESEARCH_ID,
            section_gid=RESEARCH_GID,
            section_name="Research Queue",
        ),
        bootstrap._record(
            uuid.uuid5(uuid.NAMESPACE_URL, "registry-cert-task:verification"),
            "9100000000000002",
            section_id=VERIFICATION_ID,
            section_gid=VERIFICATION_GID,
            section_name="Verification Queue",
        ),
    )
    spec = bootstrap._spec(source)
    initial_roles = {section.section_id: section.workflow_role for section in spec.sections}
    assert initial_roles[RESEARCH_ID] == f"imported-section-{RESEARCH_GID}"
    assert initial_roles[VERIFICATION_ID] == f"imported-section-{VERIFICATION_GID}"

    with session_scope(factory) as session:
        bootstrapped = bootstrap_initial_generation(
            session,
            spec,
            clock=lambda: bootstrap.NOW,
        )

    admin_run = _next(ids)
    correction_request_id = _next(ids)
    correction_arguments = {
        "research_queue_section_id": str(RESEARCH_ID),
        "verification_queue_section_id": str(VERIFICATION_ID),
    }

    with session_scope(factory) as session:
        WorkflowAuthorityService(session).register_run(
            run_id=admin_run,
            generation_id=bootstrapped.generation_id,
            owner_id="Marco",
            agent="marco",
            capability_digest=admin_run.bytes + admin_run.bytes,
            registered_at=bootstrap.NOW,
        )
        port = PostgresCommandPort(
            session,
            cursor_secret=CURSOR_SECRET,
            uuid_factory=lambda: _next(ids),
        )
        revised = port.execute(
            _call(
                "revise-section-registry",
                run_id=admin_run,
                request_id=correction_request_id,
                now=bootstrap.NOW,
                protocol_release=spec.honest.protocol_version,
                arguments=correction_arguments,
                owner_id="Marco",
                principal_class="admin",
            )
        )
        assert revised.ok is True, (revised.code, revised.http_status, revised.data)
        assert revised.data["changed"] is True

        replayed = port.execute(
            _call(
                "revise-section-registry",
                run_id=admin_run,
                request_id=correction_request_id,
                now=bootstrap.NOW,
                protocol_release=spec.honest.protocol_version,
                arguments=correction_arguments,
                owner_id="Marco",
                principal_class="admin",
            )
        )
        assert replayed.ok is True
        assert replayed.request_replayed is True
        assert replayed.data == revised.data

        retry_request_id = _next(ids)
        retried = port.execute(
            _call(
                "revise-section-registry",
                run_id=admin_run,
                request_id=retry_request_id,
                now=bootstrap.NOW,
                protocol_release=spec.honest.protocol_version,
                arguments=correction_arguments,
                owner_id="Marco",
                principal_class="admin",
            )
        )
        assert retried.ok is True
        assert retried.request_replayed is False
        assert retried.data["changed"] is False
        assert retried.data["registry_version_id"] == revised.data["registry_version_id"]
        assert retried.data["registry_activation_id"] == revised.data["registry_activation_id"]
        assert retried.data["registry_revision"] == revised.data["registry_revision"]

    with session_scope(factory) as session:
        active = session.get(models.ActiveSectionRegistry, bootstrapped.generation_id)
        generation = session.get(models.AuthorityGeneration, bootstrapped.generation_id)
        assert active is not None and generation is not None
        assert generation.status == "active"
        assert active.registry_revision == 2
        assert active.registry_version_id == uuid.UUID(str(revised.data["registry_version_id"]))
        corrected = session.get(models.SectionRegistryVersion, active.registry_version_id)
        predecessor = session.get(models.SectionRegistryVersion, bootstrapped.registry_version_id)
        assert corrected is not None and predecessor is not None
        assert corrected.version_number == predecessor.version_number + 1
        assert corrected.contract_binding_id == predecessor.contract_binding_id == bootstrapped.binding_id

        correction_import_run_id = uuid.UUID(str(revised.data["correction_import_run_id"]))
        correction = session.get(models.ImportRun, correction_import_run_id)
        assert correction is not None
        assert correction.status == "complete"
        assert correction.source_release == REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE
        assert correction.provenance["correction_kind"] == REGISTRY_ROLE_CORRECTION_KIND
        assert correction.provenance["source_import_run_id"] == str(bootstrapped.import_run_id)
        assert correction.provenance["predecessor_registry_version_id"] == str(
            bootstrapped.registry_version_id
        )
        assert correction.provenance["result_registry_sha256"] == corrected.registry_sha256
        assert correction.provenance["correction_bundle_sha256"] == correction.source_bundle_sha256
        assert correction.provenance["source_record_count"] == 0
        assert correction.provenance["requested_roles"] == {
            "research_queue": str(RESEARCH_ID),
            "verification_queue": str(VERIFICATION_ID),
        }

        correction_execution_id = uuid.UUID(str(correction.provenance["command_execution_id"]))
        execution = session.get(wf.CommandExecution, correction_execution_id)
        assert execution is not None
        assert execution.status == "committed"
        assert execution.command_name == "revise-section-registry"
        assert execution.transaction_profile == "L"
        assert execution.generation_id == bootstrapped.generation_id
        assert execution.contract_binding_id == bootstrapped.binding_id
        request = session.get(wf.ServiceRequest, execution.request_id)
        assert request is not None
        assert request.request_id == correction_request_id
        assert request.run_id == admin_run
        assert request.principal_class == "admin"
        assert request.command_name == "revise-section-registry"
        assert request.canonical_payload["arguments"] == correction_arguments
        assert registry_source_import_run(session, corrected).import_run_id == bootstrapped.import_run_id

        activation = session.get(models.SectionRegistryActivation, active.registry_activation_id)
        assert activation is not None
        assert activation.activation_route == "import"
        assert activation.import_run_id == correction_import_run_id
        assert activation.command_execution_id is None
        assert activation.registry_revision == 2

        predecessor_entries = _entries(session, predecessor.registry_version_id)
        corrected_entries = _entries(session, corrected.registry_version_id)
        predecessor_snapshot = {
            str(entry.section_id): (entry.ordinal, entry.display_name, entry.workflow_role)
            for entry in predecessor_entries
        }
        corrected_roles = {entry.section_id: entry.workflow_role for entry in corrected_entries}
        assert predecessor_snapshot[str(RESEARCH_ID)][2] == f"imported-section-{RESEARCH_GID}"
        assert predecessor_snapshot[str(VERIFICATION_ID)][2] == f"imported-section-{VERIFICATION_GID}"
        assert corrected_roles[RESEARCH_ID] == "research_queue"
        assert corrected_roles[VERIFICATION_ID] == "verification_queue"
        assert _active_alias(session, RESEARCH_ID).external_id == RESEARCH_GID
        assert _active_alias(session, VERIFICATION_ID).external_id == VERIFICATION_GID

        registry_versions = int(
            session.scalar(
                select(func.count())
                .select_from(models.SectionRegistryVersion)
                .where(models.SectionRegistryVersion.generation_id == bootstrapped.generation_id)
            )
            or 0
        )
        activations = int(
            session.scalar(
                select(func.count())
                .select_from(models.SectionRegistryActivation)
                .where(models.SectionRegistryActivation.generation_id == bootstrapped.generation_id)
            )
            or 0
        )
        assert registry_versions == 2
        assert activations == 2

        durable = {
            "generation_id": str(bootstrapped.generation_id),
            "binding_id": str(bootstrapped.binding_id),
            "predecessor_registry_version_id": str(predecessor.registry_version_id),
            "predecessor_registry_sha256": predecessor.registry_sha256,
            "predecessor_entries": predecessor_snapshot,
            "corrected_registry_version_id": str(corrected.registry_version_id),
            "corrected_registry_sha256": corrected.registry_sha256,
            "correction_import_run_id": str(correction_import_run_id),
            "registry_activation_id": str(active.registry_activation_id),
            "registry_revision": active.registry_revision,
            "command_execution_id": str(correction_execution_id),
            "command_execution_status": execution.status,
            "command_request_id": str(request.request_id),
            "command_request_principal": request.principal_class,
            "research_gid": _active_alias(session, RESEARCH_ID).external_id,
            "verification_gid": _active_alias(session, VERIFICATION_ID).external_id,
            "registry_version_count": registry_versions,
            "activation_count": activations,
        }

    fail_closed = _claimed_correction_probe(
        factory,
        bootstrapped=bootstrapped,
        protocol_release=spec.honest.protocol_version,
        research_id=RESEARCH_ID,
        verification_id=VERIFICATION_ID,
    )
    assert fail_closed == {
        "claimed": "rejected",
        "failed": "rejected",
        "uncertain": "rejected",
        "cancelled": "rejected",
    }

    with pytest.raises(IntegrityError, match="immutable authority row"):
        with session_scope(factory) as session:
            predecessor_entry = session.scalar(
                select(models.SectionRegistryEntry).where(
                    models.SectionRegistryEntry.registry_version_id
                    == bootstrapped.registry_version_id,
                    models.SectionRegistryEntry.section_id == RESEARCH_ID,
                )
            )
            assert predecessor_entry is not None
            predecessor_entry.workflow_role = "tampered"
            session.flush()

    with session_scope(factory) as session:
        predecessor_entries_after = _entries(session, bootstrapped.registry_version_id)
        assert {
            entry.section_id: entry.workflow_role for entry in predecessor_entries_after
        } == {
            RESEARCH_ID: f"imported-section-{RESEARCH_GID}",
            VERIFICATION_ID: f"imported-section-{VERIFICATION_GID}",
        }

    agent_run = _next(ids)
    create_request_id = _next(ids)
    start_request_id = _next(ids)
    prepare_request_id = _next(ids)
    with session_scope(factory) as session:
        WorkflowAuthorityService(session).register_run(
            run_id=agent_run,
            generation_id=bootstrapped.generation_id,
            owner_id="owner-1",
            agent="claude",
            capability_digest=agent_run.bytes + agent_run.bytes,
            registered_at=bootstrap.NOW,
        )
        ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
            generation_id=bootstrapped.generation_id,
            activation_reason="registry correction certification role-consumption proof",
            created_at=bootstrap.NOW,
            external_effects_enabled=True,
        )
        port = PostgresCommandPort(
            session,
            cursor_secret=CURSOR_SECRET,
            uuid_factory=lambda: _next(ids),
        )
        created = port.execute(
            _call(
                "create",
                run_id=agent_run,
                request_id=create_request_id,
                now=bootstrap.NOW,
                protocol_release=spec.honest.protocol_version,
                arguments={"title": "Registry correction certification dish"},
            )
        )
        assert created.ok is True, (created.code, created.http_status, created.data)
        task_id = uuid.UUID(str(created.data["task_id"]))
        research_placement = session.get(
            models.CurrentTaskSectionPlacement,
            (bootstrapped.generation_id, task_id),
        )
        assert research_placement is not None
        assert research_placement.section_id == RESEARCH_ID
        assert research_placement.registry_version_id == uuid.UUID(
            str(revised.data["registry_version_id"])
        )
        assert _active_alias(session, research_placement.section_id).external_id == RESEARCH_GID

        started = port.execute(
            _call(
                "start",
                run_id=agent_run,
                request_id=start_request_id,
                now=bootstrap.NOW,
                protocol_release=spec.honest.protocol_version,
                arguments={
                    "task_id": str(task_id),
                    "kind": "initial",
                    "agent": "claude",
                },
            )
        )
        assert started.ok is True, (started.code, started.http_status, started.data)
        prepared = port.execute(
            _call(
                "prepare",
                run_id=agent_run,
                request_id=prepare_request_id,
                now=bootstrap.NOW,
                protocol_release=spec.honest.protocol_version,
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "file_text": TASK,
                    "agent": "claude",
                    "model": "registry-cert-model",
                },
            )
        )
        assert prepared.ok is True, (prepared.code, prepared.http_status, prepared.data)
        verification_placement = session.get(
            models.CurrentTaskSectionPlacement,
            (bootstrapped.generation_id, task_id),
        )
        assert verification_placement is not None
        assert verification_placement.section_id == VERIFICATION_ID
        assert verification_placement.registry_version_id == uuid.UUID(
            str(revised.data["registry_version_id"])
        )
        assert _active_alias(session, verification_placement.section_id).external_id == VERIFICATION_GID

        consumability = {
            "create_request_id": str(create_request_id),
            "task_id": str(task_id),
            "create_section_id": str(research_placement.section_id),
            "create_section_gid": RESEARCH_GID,
            "start_request_id": str(start_request_id),
            "operation_id": str(started.data["operation_id"]),
            "prepare_request_id": str(prepare_request_id),
            "prepare_section_id": str(verification_placement.section_id),
            "prepare_section_gid": VERIFICATION_GID,
            "registry_version_id": str(verification_placement.registry_version_id),
        }

    evidence = {
        "format": "dish-registry-correction-certification-v1",
        "certified_implementation_sha": CERTIFIED_IMPLEMENTATION_SHA,
        "runtime": runtime,
        "setup": {
            "research_queue": {
                "section_id": str(RESEARCH_ID),
                "asana_gid": RESEARCH_GID,
                "initial_workflow_role": initial_roles[RESEARCH_ID],
            },
            "verification_queue": {
                "section_id": str(VERIFICATION_ID),
                "asana_gid": VERIFICATION_GID,
                "initial_workflow_role": initial_roles[VERIFICATION_ID],
            },
            "initial_registry_version_id": str(bootstrapped.registry_version_id),
            "initial_registry_revision": 1,
        },
        "durable_correction": durable,
        "provenance_fail_closed": fail_closed,
        "immutability": {"predecessor_tamper": "rejected_by_native_postgresql_trigger"},
        "replay_retry": {
            "same_request_replayed": replayed.request_replayed,
            "same_request_result_identical": replayed.data == revised.data,
            "fresh_retry_changed": retried.data["changed"],
            "fresh_retry_registry_version_id": retried.data["registry_version_id"],
            "fresh_retry_registry_revision": retried.data["registry_revision"],
            "registry_version_count_after_retry": durable["registry_version_count"],
            "activation_count_after_retry": durable["activation_count"],
        },
        "role_consumability": consumability,
    }
    output = Path(os.environ["REGISTRY_CORRECTION_CERT_EVIDENCE"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("REGISTRY_CORRECTION_CERT_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
