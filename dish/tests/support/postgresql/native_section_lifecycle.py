"""Shared helpers for native Section catalog and lifecycle PostgreSQL tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from dish_pg import models
from dish_pg.command_port import CommandCall
from dish_pg.native_section_carry_forward import RepositoryIdentity
from dish_pg.repositories import CatalogRepository
from tests.support.postgresql.native_section_content_materializer_fixtures import (
    _stage_pr3,
)

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)


def _stage_runtime_switch_fixture(session, ids, monkeypatch, **fixture_kwargs):
    monkeypatch.setattr(
        "dish_pg.native_section_carry_forward._verified_repository_identity",
        lambda: RepositoryIdentity(commit_sha="a" * 40, tree_sha="b" * 40),
    )
    return _stage_pr3(session, ids, **fixture_kwargs)


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
