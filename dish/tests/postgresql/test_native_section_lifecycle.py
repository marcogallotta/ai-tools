from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg.command_port import CommandCall
from dish_pg.database import session_scope
from dish_pg.native_catalog_runtime_finalizer import finalize_native_catalog_runtime_authority
from dish_pg.read_model import ReadModelError
from dish_pg.repositories import CatalogRepository
from tests.postgresql.test_native_section_catalog_foundation import (
    NOW,
    _stage_runtime_switch_fixture,
)
from tests.support.postgresql.command import _port
from tests.support.postgresql.workflow import _next, _register_run

pytestmark = pytest.mark.database_boundary
pytest_plugins = ("tests.support.postgresql.core",)


def _view(session, generation_id: uuid.UUID) -> dict[str, object]:
    active = session.get(models.ActiveSectionCatalog, generation_id)
    pointer = session.get(models.CurrentNativeCatalogRuntime, generation_id)
    assert active is not None and pointer is not None
    return {
        "expected_catalog_version_id": str(active.catalog_version_id),
        "expected_catalog_activation_id": str(active.catalog_activation_id),
        "expected_catalog_revision": active.catalog_revision,
        "expected_runtime_attestation_id": str(pointer.attestation_id),
        "expected_runtime_attestation_revision": pointer.attestation_revision,
    }


def _call(session, command, *, run_id, request_id, generation_id, arguments):
    contract = CatalogRepository(session).active_runtime_catalog_contract(generation_id)
    assert contract is not None
    return CommandCall(
        command_name=command,
        arguments=arguments,
        owner_id="Marco",
        principal_class="admin",
        run_id=run_id,
        request_id=request_id,
        now=NOW + timedelta(hours=2),
        protocol_release=contract.honest_binding.protocol_release,
    )


def _runtime_fixture(session, ids, monkeypatch):
    seeded, _expectation, _, _carry_event_id, _occurrences = _stage_runtime_switch_fixture(
        session, ids, monkeypatch
    )
    finalize_native_catalog_runtime_authority(
        session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
    )
    run_id = _next(ids)
    _register_run(
        session,
        generation_id=seeded["generation_id"],
        run_id=run_id,
        owner="Marco",
    )
    return seeded, run_id


def test_create_rebinds_catalog_only_and_exact_replay_is_effect_free(
    core_db, monkeypatch
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, run_id = _runtime_fixture(session, ids, monkeypatch)
        generation_id = seeded["generation_id"]
        states_before = {
            state.task_id: (
                state.catalog_version_id,
                state.section_id,
                state.dish_version,
                state.placement_version,
                state.current_content_version_id,
            )
            for state in session.scalars(
                select(models.DishState).where(models.DishState.generation_id == generation_id)
            )
        }
        view = _view(session, generation_id)
        request_id = _next(ids)
        port = _port(session, ids)
        call = _call(
            session,
            "create-section",
            run_id=run_id,
            request_id=request_id,
            generation_id=generation_id,
            arguments={**view, "display_name": "Bread Lab"},
        )
        created = port.execute(call)
        assert created.ok, (created.code, created.data)
        section_id = uuid.UUID(created.data["section_id"])
        assert created.data["rebound_dish_count"] == len(states_before)
        assert session.get(models.Section, section_id).lifecycle == "active"

        for state in session.scalars(
            select(models.DishState).where(models.DishState.generation_id == generation_id)
        ):
            old = states_before[state.task_id]
            assert state.catalog_version_id == uuid.UUID(created.data["catalog_version_id"])
            assert state.section_id == old[1]
            assert state.dish_version == old[2]
            assert state.placement_version == old[3]
            assert state.current_content_version_id == old[4]
            receipt = session.get(
                models.SectionCatalogRebindReceipt,
                (generation_id, state.task_id, state.catalog_version_id),
            )
            assert receipt is not None
            assert receipt.predecessor_catalog_version_id == old[0]
            assert receipt.section_id == state.section_id

        versions = session.scalar(
            select(func.count()).select_from(models.SectionCatalogVersion).where(
                models.SectionCatalogVersion.generation_id == generation_id
            )
        )
        attestations = session.scalar(
            select(func.count()).select_from(models.NativeCatalogRuntimeAttestation).where(
                models.NativeCatalogRuntimeAttestation.generation_id == generation_id
            )
        )
        replay = port.execute(call)
        assert replay.ok and replay.request_replayed is True
        assert replay.data == created.data
        assert session.scalar(
            select(func.count()).select_from(models.SectionCatalogVersion).where(
                models.SectionCatalogVersion.generation_id == generation_id
            )
        ) == versions
        assert session.scalar(
            select(func.count()).select_from(models.NativeCatalogRuntimeAttestation).where(
                models.NativeCatalogRuntimeAttestation.generation_id == generation_id
            )
        ) == attestations
        catalog_version_id = uuid.UUID(created.data["catalog_version_id"])

    with session_scope(factory) as session:
        contract = CatalogRepository(session).active_runtime_catalog_contract(generation_id)
        assert contract is not None
        assert contract.catalog_version.catalog_version_id == catalog_version_id
        assert next(
            entry for entry in contract.entries if entry.section_id == section_id
        ).display_name == "Bread Lab"


def test_rename_preserves_identity_role_order_and_old_history(core_db, monkeypatch) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, run_id = _runtime_fixture(session, ids, monkeypatch)
        generation_id = seeded["generation_id"]
        contract = CatalogRepository(session).active_runtime_catalog_contract(generation_id)
        assert contract is not None
        target = next(entry for entry in contract.entries if entry.workflow_role == "research_queue")
        old_catalog_id = contract.catalog_version.catalog_version_id
        port = _port(session, ids)
        renamed = port.execute(
            _call(
                session,
                "rename-section",
                run_id=run_id,
                request_id=_next(ids),
                generation_id=generation_id,
                arguments={
                    **_view(session, generation_id),
                    "section_id": str(target.section_id),
                    "display_name": "Research Intake",
                },
            )
        )
        assert renamed.ok, (renamed.code, renamed.data)
        current = CatalogRepository(session).active_runtime_catalog_contract(generation_id)
        assert current is not None
        revised = next(entry for entry in current.entries if entry.section_id == target.section_id)
        assert revised.display_name == "Research Intake"
        assert revised.workflow_role == target.workflow_role
        assert revised.ordinal == target.ordinal
        historical = session.get(
            models.SectionCatalogEntry, (old_catalog_id, target.section_id)
        )
        assert historical is not None and historical.display_name == target.display_name


def test_retire_refuses_nonempty_and_required_then_retires_empty_section(
    core_db, monkeypatch
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, run_id = _runtime_fixture(session, ids, monkeypatch)
        generation_id = seeded["generation_id"]
        port = _port(session, ids)
        resident = session.scalar(
            select(models.DishState).where(models.DishState.generation_id == generation_id)
        )
        assert resident is not None
        before = _view(session, generation_id)
        nonempty = port.execute(
            _call(
                session,
                "retire-section",
                run_id=run_id,
                request_id=_next(ids),
                generation_id=generation_id,
                arguments={**before, "section_id": str(resident.section_id)},
            )
        )
        assert not nonempty.ok and nonempty.code == "SECTION_NOT_EMPTY"
        assert _view(session, generation_id) == before

        contract = CatalogRepository(session).active_runtime_catalog_contract(generation_id)
        assert contract is not None
        required = next(entry for entry in contract.entries if entry.workflow_role == "verification_queue")
        protected = port.execute(
            _call(
                session,
                "retire-section",
                run_id=run_id,
                request_id=_next(ids),
                generation_id=generation_id,
                arguments={**_view(session, generation_id), "section_id": str(required.section_id)},
            )
        )
        assert not protected.ok and protected.code == "SECTION_REQUIRED_BY_WORKFLOW"

        created = port.execute(
            _call(
                session,
                "create-section",
                run_id=run_id,
                request_id=_next(ids),
                generation_id=generation_id,
                arguments={**_view(session, generation_id), "display_name": "Temporary"},
            )
        )
        assert created.ok
        empty_id = uuid.UUID(created.data["section_id"])
        retired = port.execute(
            _call(
                session,
                "retire-section",
                run_id=run_id,
                request_id=_next(ids),
                generation_id=generation_id,
                arguments={**_view(session, generation_id), "section_id": str(empty_id)},
            )
        )
        assert retired.ok, (retired.code, retired.data)
        section = session.get(models.Section, empty_id)
        assert section is not None and section.lifecycle == "retired"
        current = CatalogRepository(session).active_runtime_catalog_contract(generation_id)
        assert current is not None
        assert empty_id not in {entry.section_id for entry in current.entries}
        with pytest.raises(ReadModelError):
            port.reads.resolve_section(str(empty_id))


def test_stale_catalog_view_fails_closed(core_db, monkeypatch) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, run_id = _runtime_fixture(session, ids, monkeypatch)
        generation_id = seeded["generation_id"]
        stale = _view(session, generation_id)
        port = _port(session, ids)
        first = port.execute(
            _call(
                session,
                "create-section",
                run_id=run_id,
                request_id=_next(ids),
                generation_id=generation_id,
                arguments={**stale, "display_name": "First"},
            )
        )
        assert first.ok
        revision = first.data["catalog_revision"]
        loser = port.execute(
            _call(
                session,
                "create-section",
                run_id=run_id,
                request_id=_next(ids),
                generation_id=generation_id,
                arguments={**stale, "display_name": "Stale"},
            )
        )
        assert not loser.ok and loser.code == "STALE_CATALOG_VIEW"
        assert _view(session, generation_id)["expected_catalog_revision"] == revision
