from __future__ import annotations

import uuid

import pytest

from dish_pg.database import session_scope
from dish_pg.revise_section_registry import (
    ReviseSectionRegistryError,
    revise_section_registry,
)
from tests.support.postgresql.command import _add_destination_section
from tests.support.postgresql.workflow import NOW, workflow_db

SECRET = b"registry-correction-trigger-secret-32b!"


def test_revise_section_registry_assigns_both_roles(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        verification_section_id = _add_destination_section(
            session, ids, context, external_id="1217084805070799"
        )
        result = revise_section_registry(
            session,
            research_queue_section_id=context["section_id"],
            verification_queue_section_id=verification_section_id,
            owner_id="Marco",
            agent="marco",
            cursor_secret=SECRET,
            now=NOW,
            uuid_factory=lambda: next(ids),
        )
        assert result.ok is True, (result.code, result.http_status, result.data)
        assert result.data["changed"] is True


def test_revise_section_registry_rejects_identical_sections(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        with pytest.raises(ReviseSectionRegistryError):
            revise_section_registry(
                session,
                research_queue_section_id=context["section_id"],
                verification_queue_section_id=context["section_id"],
                owner_id="Marco",
                agent="marco",
                cursor_secret=SECRET,
                now=NOW,
                uuid_factory=lambda: next(ids),
            )


def test_revise_section_registry_is_idempotent_on_rerun(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        verification_section_id = _add_destination_section(
            session, ids, context, external_id="1217084805070799"
        )
        first = revise_section_registry(
            session,
            research_queue_section_id=context["section_id"],
            verification_queue_section_id=verification_section_id,
            owner_id="Marco",
            agent="marco",
            cursor_secret=SECRET,
            now=NOW,
            uuid_factory=lambda: next(ids),
        )
        assert first.ok is True
        second = revise_section_registry(
            session,
            research_queue_section_id=context["section_id"],
            verification_queue_section_id=verification_section_id,
            owner_id="Marco",
            agent="marco",
            cursor_secret=SECRET,
            now=NOW,
            uuid_factory=lambda: next(ids),
        )
        assert second.ok is True
        assert second.data["changed"] is False
