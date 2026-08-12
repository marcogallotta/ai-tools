"""First-generation bootstrap and real importer-hook coverage."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.bootstrap import (
    DEFAULT_SCHEMA_HEAD,
    DEFAULT_PROJECT_GID,
    DEFAULT_PROJECT_ID,
    HonestCheckout,
    InitialBootstrapError,
    InitialBootstrapSpec,
    apply_research_queue_role,
    apply_verification_queue_role,
    bootstrap_initial_generation,
    inspect_source_bundle,
    resolve_honest_checkout,
    section_specs_from_bundle,
)
from dish_pg.database import session_scope
from dish_pg.import_runtime import already_imported, prepare_import_run, verify_imported_records
from dish_pg.importer import iter_source, run_import
from dish_pg.release_history import (
    EXACT_REVOCATION_HISTORY_PROVENANCE_KEY,
    EXACT_REVOCATION_SOURCE_CONTRACT,
)
from dish_pg.repositories import (
    CoreAuthorityError,
    REGISTRY_ROLE_CORRECTION_KIND,
    REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE,
    registry_source_import_run,
)
from dish_pg.transition import ShadowService
from dish_pg.workflow import WorkflowAuthorityService

NOW = datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)

DEFAULT_SECTION_GID = "1216891250619908"
DEFAULT_SECTION_ID = uuid.UUID("8b5bfb31-b986-5116-a207-569a5ba95907")

OTHER_SECTION_GID = "1216891250619999"
OTHER_SECTION_ID = uuid.UUID("2a6f8b2e-4f3a-5c1e-9a2d-6e7f8a9b0c1d")


def _record(
    task_id: uuid.UUID,
    gid: str,
    *,
    title: str = "[ready] Imported",
    section_id: uuid.UUID = DEFAULT_SECTION_ID,
    section_gid: str = DEFAULT_SECTION_GID,
    section_name: str = "Research Queue",
) -> dict[str, object]:
    return {
        "task_id": str(task_id),
        "asana_task_gid": gid,
        "title": title,
        "body": "Canonical body\n---\nStatus: ready\n",
        "identity_scheme": "legacy-sha256-v1",
        "content_identity": hashlib.sha256(gid.encode()).hexdigest(),
        "project_ids": [str(DEFAULT_PROJECT_ID)],
        "section_id": str(section_id),
        "section_gid": section_gid,
        "section_name": section_name,
        "completed": False,
        "observed_at": NOW.isoformat(),
        "operation_history": {"operations": [], "leases": [], "verification_cycles": [], "revocations": []},
    }


def _source(tmp_path: Path, *records: dict[str, object]) -> Path:
    path = tmp_path / "legacy.ndjson"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _spec(source: Path) -> InitialBootstrapSpec:
    bundle = inspect_source_bundle(
        source,
        project_id=DEFAULT_PROJECT_ID,
    )
    honest = HonestCheckout(
        root=source.parent,
        commit="f" * 40,
        protocol_version="1.0.10",
        schema_version="2",
        protocol_sha256="1" * 64,
        schema_sha256="2" * 64,
        protocol_files={"planning": {"path": "dish-planning-protocol.md", "sha256": "3" * 64}},
    )
    return InitialBootstrapSpec(
        dish_commit="9" * 40,
        schema_head=DEFAULT_SCHEMA_HEAD,
        source_generation="test-dark-launch-rehearsal-2026-08-03",
        source_bundle=bundle,
        honest=honest,
        project_id=DEFAULT_PROJECT_ID,
        project_gid=DEFAULT_PROJECT_GID,
        project_name="Cooking",
        sections=section_specs_from_bundle(bundle),
    )


def _factory(tmp_path: Path) -> tuple[sessionmaker[Session], object]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'authority.sqlite'}", future=True)
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    return factory, engine


@pytest.mark.smoke
def test_bootstrap_baseline_and_import_run_end_to_end(tmp_path: Path) -> None:
    first_task, second_task = uuid.uuid4(), uuid.uuid4()
    operation_id, cycle_id, lease_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first = _record(first_task, "1001")
    first["operation_history"] = {
        "operations": [{
            "operation_id": str(operation_id), "kind": "planning", "status": "completed",
            "phase": "terminal", "terminal_outcome": "planning_handoff_confirmed",
            "created_at": NOW.isoformat(), "completed_at": NOW.isoformat(),
        }],
        "verification_cycles": [{
            "cycle_id": str(cycle_id), "operation_id": str(operation_id),
            "cycle_sequence": 1, "outcome": "approved",
            "created_at": NOW.isoformat(), "completed_at": NOW.isoformat(),
        }],
        "leases": [{
            "lease_id": str(lease_id), "operation_id": str(operation_id),
            "source_run_id": "legacy-run-1", "owner_id": "legacy-owner",
            "lease_kind": "actor", "actor_attempt_sequence": 1,
            "verification_cycle_id": str(cycle_id), "issued_at": NOW.isoformat(),
            "expires_at": "2026-08-03T20:05:00+00:00", "released_at": NOW.isoformat(),
        }],
        "revocations": [],
    }
    source = _source(tmp_path, first, _record(second_task, "1002"))
    spec = _spec(source)
    factory, engine = _factory(tmp_path)
    try:
        with session_scope(factory) as session:
            result = bootstrap_initial_generation(session, spec, clock=lambda: NOW)
        with session_scope(factory) as session:
            generation = session.get(models.AuthorityGeneration, result.generation_id)
            import_run = session.get(models.ImportRun, result.import_run_id)
            binding = session.get(models.HonestContractBinding, result.binding_id)
            assert generation is not None
            assert generation.status == "active"
            assert generation.predecessor_generation_id is None
            assert generation.creation_reason == "initial_cutover"
            assert generation.schema_head == DEFAULT_SCHEMA_HEAD
            assert import_run is not None
            assert import_run.source_bundle_sha256 == spec.source_bundle.sha256
            assert import_run.baseline_high_water_mark == spec.source_bundle.high_water_mark
            assert import_run.provenance[EXACT_REVOCATION_HISTORY_PROVENANCE_KEY] == (
                EXACT_REVOCATION_SOURCE_CONTRACT
            )
            assert binding is not None
            assert binding.protocol_release == "1.0.10"
            assert binding.schema_release == "2"
            assert session.get(models.GovernedProject, DEFAULT_PROJECT_ID) is not None
            governed_section = session.get(models.GovernedSection, DEFAULT_SECTION_ID)
            assert governed_section is not None
            assert governed_section.logical_name == "Research Queue"
            registry_entry = session.scalar(
                select(models.SectionRegistryEntry).where(
                    models.SectionRegistryEntry.registry_version_id == result.registry_version_id,
                    models.SectionRegistryEntry.section_id == DEFAULT_SECTION_ID,
                )
            )
            assert registry_entry is not None
            assert registry_entry.display_name == "Research Queue"
            baseline = ShadowService(session).create_baseline(
                generation_id=result.generation_id,
                source_generation_identity=spec.source_generation,
                source_commit=spec.dish_commit,
                created_at=NOW,
            )
            assert baseline.generation_id == result.generation_id

        summary = run_import(
            source=source,
            generation_id=result.generation_id,
            import_run_id=result.import_run_id,
            contract_binding_id=result.binding_id,
            session_factory=factory,
            prepare_import_run=prepare_import_run,
            already_imported=already_imported,
        )
        assert (summary.imported, summary.skipped, summary.failed) == (2, 0, 0)
        records = tuple(iter_source(source))
        with session_scope(factory) as session:
            assert verify_imported_records(
                session,
                records=records,
                generation_id=result.generation_id,
                import_run_id=result.import_run_id,
                contract_binding_id=result.binding_id,
            ) == []
            assert int(session.scalar(select(func.count()).select_from(models.DishTask)) or 0) == 2
            assert int(session.scalar(select(func.count()).select_from(models.TaskExternalAlias)) or 0) == 2
    finally:
        engine.dispose()


def test_bootstrap_refuses_nonempty_authority_target(tmp_path: Path) -> None:
    source = _source(tmp_path, _record(uuid.uuid4(), "2001"))
    spec = _spec(source)
    factory, engine = _factory(tmp_path)
    try:
        with session_scope(factory) as session:
            bootstrap_initial_generation(session, spec, clock=lambda: NOW)
        with pytest.raises(InitialBootstrapError, match="requires empty authority tables"):
            with session_scope(factory) as session:
                bootstrap_initial_generation(session, spec, clock=lambda: NOW)
        with session_scope(factory) as session:
            assert int(session.scalar(select(func.count()).select_from(models.AuthorityGeneration)) or 0) == 1
    finally:
        engine.dispose()


def test_source_bundle_registers_every_distinct_section(tmp_path: Path) -> None:
    first_task, second_task = uuid.uuid4(), uuid.uuid4()
    source = _source(
        tmp_path,
        _record(first_task, "3001"),
        _record(
            second_task,
            "3002",
            section_id=OTHER_SECTION_ID,
            section_gid=OTHER_SECTION_GID,
            section_name="Verification Queue",
        ),
    )
    bundle = inspect_source_bundle(source, project_id=DEFAULT_PROJECT_ID)
    assert bundle.sections == {
        DEFAULT_SECTION_ID: DEFAULT_SECTION_GID,
        OTHER_SECTION_ID: OTHER_SECTION_GID,
    }
    assert bundle.section_names == {
        DEFAULT_SECTION_ID: "Research Queue",
        OTHER_SECTION_ID: "Verification Queue",
    }
    assert bundle.explicit_operation_revocations is True
    sections = section_specs_from_bundle(bundle)
    assert [section.section_id for section in sections] == [DEFAULT_SECTION_ID, OTHER_SECTION_ID]
    assert [section.section_name for section in sections] == ["Research Queue", "Verification Queue"]
    assert len({section.workflow_role for section in sections}) == len(sections)


def test_bootstrap_role_assignment_is_explicit_for_both_special_queues(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        _record(uuid.uuid4(), "3010"),
        _record(
            uuid.uuid4(),
            "3011",
            section_id=OTHER_SECTION_ID,
            section_gid=OTHER_SECTION_GID,
            section_name="Verification Queue",
        ),
    )
    sections = section_specs_from_bundle(
        inspect_source_bundle(source, project_id=DEFAULT_PROJECT_ID)
    )
    sections = apply_research_queue_role(
        sections, research_queue_section_id=DEFAULT_SECTION_ID
    )
    sections = apply_verification_queue_role(
        sections, verification_queue_section_id=OTHER_SECTION_ID
    )
    assert {section.workflow_role for section in sections} >= {
        "research_queue", "verification_queue"
    }


def test_source_bundle_requires_explicit_revocation_history_field(tmp_path: Path) -> None:
    record = _record(uuid.uuid4(), "3002")
    history = dict(record["operation_history"])
    history.pop("revocations")
    record["operation_history"] = history
    source = _source(tmp_path, record)
    with pytest.raises(InitialBootstrapError, match="operation_history.revocations is required"):
        inspect_source_bundle(source, project_id=DEFAULT_PROJECT_ID)


def test_source_bundle_rejects_section_id_reused_for_a_different_gid(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        _record(uuid.uuid4(), "3003"),
        _record(uuid.uuid4(), "3004", section_gid="9999999999999999"),
    )
    with pytest.raises(InitialBootstrapError, match="maps to both"):
        inspect_source_bundle(source, project_id=DEFAULT_PROJECT_ID)


def test_source_bundle_rejects_section_id_reused_for_a_different_name(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        _record(uuid.uuid4(), "3005"),
        _record(uuid.uuid4(), "3006", section_name="Different name"),
    )
    with pytest.raises(InitialBootstrapError, match="section_name"):
        inspect_source_bundle(source, project_id=DEFAULT_PROJECT_ID)


def test_registry_revision_uses_truthful_correction_import_without_rewriting_authority(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        _record(uuid.uuid4(), "3901"),
        _record(
            uuid.uuid4(),
            "3902",
            section_id=OTHER_SECTION_ID,
            section_gid=OTHER_SECTION_GID,
            section_name="Verification Queue",
        ),
    )
    base_spec = _spec(source)
    spec = replace(
        base_spec,
        sections=apply_research_queue_role(
            base_spec.sections, research_queue_section_id=DEFAULT_SECTION_ID
        ),
    )
    factory, engine = _factory(tmp_path)
    try:
        with session_scope(factory) as session:
            bootstrapped = bootstrap_initial_generation(session, spec, clock=lambda: NOW)
        admin_run = uuid.uuid4()
        request_id = uuid.uuid4()
        with session_scope(factory) as session:
            WorkflowAuthorityService(session).register_run(
                run_id=admin_run,
                generation_id=bootstrapped.generation_id,
                owner_id="Marco",
                agent="marco",
                capability_digest=b"r" * 32,
                registered_at=NOW,
            )
            revised = PostgresCommandPort(
                session, cursor_secret=b"registry-revision-test-secret-32b"
            ).execute(
                CommandCall(
                    command_name="revise-section-registry",
                    arguments={
                        "research_queue_section_id": str(DEFAULT_SECTION_ID),
                        "verification_queue_section_id": str(OTHER_SECTION_ID),
                    },
                    owner_id="Marco",
                    principal_class="admin",
                    run_id=admin_run,
                    request_id=request_id,
                    now=NOW,
                    protocol_release=spec.honest.protocol_version,
                )
            )
            assert revised.ok is True
            assert revised.data["changed"] is True

            active = session.get(
                models.ActiveSectionRegistry, bootstrapped.generation_id
            )
            assert active is not None
            assert active.registry_revision == 2
            version = session.get(
                models.SectionRegistryVersion, active.registry_version_id
            )
            assert version is not None
            correction_import_run_id = uuid.UUID(
                str(revised.data["correction_import_run_id"])
            )
            assert version.import_run_id == correction_import_run_id
            correction = session.get(models.ImportRun, correction_import_run_id)
            assert correction is not None
            assert correction.status == "complete"
            assert correction.source_release == REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE
            assert correction.provenance["correction_kind"] == REGISTRY_ROLE_CORRECTION_KIND
            assert correction.provenance["source_import_run_id"] == str(
                bootstrapped.import_run_id
            )
            assert correction.provenance["predecessor_registry_version_id"] == str(
                bootstrapped.registry_version_id
            )
            assert correction.provenance["result_registry_sha256"] == version.registry_sha256
            assert (
                correction.provenance["correction_bundle_sha256"]
                == correction.source_bundle_sha256
            )
            assert correction.provenance["source_record_count"] == 0
            correction_execution_id = uuid.UUID(
                str(correction.provenance["command_execution_id"])
            )
            correction_execution = session.get(
                wf.CommandExecution, correction_execution_id
            )
            assert correction_execution is not None
            assert correction_execution.command_name == "revise-section-registry"
            assert correction_execution.status == "committed"
            correction_request = session.get(
                wf.ServiceRequest, correction_execution.request_id
            )
            assert correction_request is not None
            assert correction_request.principal_class == "admin"
            assert correction_request.command_name == "revise-section-registry"
            assert correction_request.canonical_payload["arguments"] == {
                "research_queue_section_id": str(DEFAULT_SECTION_ID),
                "verification_queue_section_id": str(OTHER_SECTION_ID),
            }
            assert registry_source_import_run(session, version).import_run_id == (
                bootstrapped.import_run_id
            )
            activation = session.get(
                models.SectionRegistryActivation, active.registry_activation_id
            )
            assert activation is not None
            assert activation.activation_route == "import"
            assert activation.import_run_id == correction_import_run_id
            assert activation.command_execution_id is None

            entries = list(
                session.scalars(
                    select(models.SectionRegistryEntry).where(
                        models.SectionRegistryEntry.registry_version_id
                        == active.registry_version_id
                    )
                )
            )
            roles = {entry.section_id: entry.workflow_role for entry in entries}
            assert roles[DEFAULT_SECTION_ID] == "research_queue"
            assert roles[OTHER_SECTION_ID] == "verification_queue"
            original = list(
                session.scalars(
                    select(models.SectionRegistryEntry).where(
                        models.SectionRegistryEntry.registry_version_id
                        == bootstrapped.registry_version_id
                    )
                )
            )
            assert {entry.workflow_role for entry in original} == {
                "research_queue",
                f"imported-section-{OTHER_SECTION_GID}",
            }
            original_version = session.get(
                models.SectionRegistryVersion, bootstrapped.registry_version_id
            )
            assert original_version is not None
            assert original_version.import_run_id == bootstrapped.import_run_id
            with pytest.raises(
                CoreAuthorityError, match="exact-operation revocation source"
            ):
                prepare_import_run(
                    session,
                    bootstrapped.generation_id,
                    correction_import_run_id,
                    bootstrapped.binding_id,
                )

            original_provenance = dict(correction.provenance)
            original_bundle_sha256 = correction.source_bundle_sha256
            fake_execution_id = uuid.uuid4()
            tampered_payload = {
                "format": "dish-registry-role-correction-v1",
                "generation_id": str(version.generation_id),
                "predecessor_registry_version_id": original_provenance[
                    "predecessor_registry_version_id"
                ],
                "source_import_run_id": original_provenance["source_import_run_id"],
                "command_execution_id": str(fake_execution_id),
                "requested_roles": original_provenance["requested_roles"],
                "result_registry_sha256": version.registry_sha256,
            }
            tampered_bundle_sha256 = hashlib.sha256(
                json.dumps(
                    tampered_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            correction.provenance = {
                **original_provenance,
                "command_execution_id": str(fake_execution_id),
                "correction_bundle_sha256": tampered_bundle_sha256,
            }
            correction.source_bundle_sha256 = tampered_bundle_sha256
            session.flush()
            with pytest.raises(
                CoreAuthorityError, match="command execution provenance"
            ):
                registry_source_import_run(session, version)

            correction.provenance = {
                **original_provenance,
                "source_record_count": 1,
            }
            correction.source_bundle_sha256 = original_bundle_sha256
            session.flush()
            with pytest.raises(
                CoreAuthorityError, match="cannot claim imported source records"
            ):
                registry_source_import_run(session, version)

            correction.provenance = original_provenance
            correction.source_bundle_sha256 = original_bundle_sha256
            session.flush()
    finally:
        engine.dispose()


def test_honest_checkout_uses_real_git_and_asset_hashes(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "honest-pantry"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "DISH_VERSION").write_text("PROTOCOL_VERSION=1.0.10\nSCHEMA_VERSION=2\n", encoding="utf-8")
    schema = {
        "protocol_version": "1.0.10",
        "schema_version": "2",
        "protocol_files": {
            "planning": "dish-planning-protocol.md",
            "research": "dish-research-protocol.md",
            "verification": "dish-verification-protocol.md",
            "cooking": "dish-cooking-protocol.md",
        },
    }
    (repo / "dish-task-schema.json").write_text(json.dumps(schema), encoding="utf-8")
    for role, filename in schema["protocol_files"].items():
        (repo / filename).write_text(f"{role} protocol\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    monkeypatch.setattr(
        "dish_pg.bootstrap.validate_task_schema_shape",
        lambda value, filename: value,
    )
    resolved = resolve_honest_checkout(
        repo, expected_commit=commit, expected_protocol_version="1.0.10"
    )
    assert resolved.commit == commit
    assert resolved.schema_sha256 == hashlib.sha256((repo / "dish-task-schema.json").read_bytes()).hexdigest()
    assert set(resolved.protocol_files) == {"planning", "research", "verification", "cooking"}
    assert len(resolved.protocol_sha256) == 64
