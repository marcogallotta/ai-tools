from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from dish_pg import models, test_comparator as comparator
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.read_model import PostgresReadModel
from dish_pg.revise_section_registry import (
    ReviseSectionRegistryError,
    ReviseTestSectionRegistryMembershipError,
    TEST_DATABASE_NAME,
    _revise_test_section_registry_membership_transaction,
    require_test_database_url,
    revise_section_registry,
    revise_test_section_registry_membership,
)
from tests.support.postgresql.command import _add_destination_section
from tests.support.postgresql.workflow import NOW, workflow_db

SECRET = b"registry-membership-regression-secret!"
# The bootstrap fixture intentionally starts Research Queue on a different external
# identity (1217084805070731).  This target therefore exercises the TEST repair
# case where the governed Research section and its task placements must be kept
# while its stale predecessor Asana alias is replaced by the exact live identity.
GIDS = ("1217084805070732", "1217084805070799", "1217084805070800", "1217084805070801")
NAMES = ("Research Queue", "Verification Queue", "Sourcing", "Reference")
PLAN = Path(__file__).resolve().parents[2] / "deploy/comparator/test-qualification-plan.json"


def _call(session, ids, context, **overrides):
    values = {
        "target_database_name": TEST_DATABASE_NAME,
        "expected_generation_id": context["generation_id"],
        "expected_registry_version_id": context["registry_version_id"],
        "expected_registry_revision": 1,
        "research_queue_section_gid": GIDS[0],
        "verification_queue_section_gid": GIDS[1],
        "sourcing_section_gid": GIDS[2],
        "reference_section_gid": GIDS[3],
        "owner_id": "Marco",
        "agent": "marco",
        "now": NOW,
        "uuid_factory": lambda: next(ids),
    }
    values.update(overrides)
    return _revise_test_section_registry_membership_transaction(session, **values)


def _counts(session):
    return tuple(
        int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in (
            wf.ServiceRun,
            models.ImportRun,
            models.GovernedSection,
            models.SectionExternalAlias,
            models.SectionRegistryVersion,
            models.SectionRegistryEntry,
            models.SectionRegistryActivation,
        )
    )


def _active(session, generation_id):
    row = session.get(models.ActiveSectionRegistry, generation_id)
    assert row is not None
    return row.registry_version_id, row.registry_revision


def test_test_membership_revision_creates_exact_four_preserves_history_and_matches_comparator(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        old_entries = tuple(session.scalars(select(models.SectionRegistryEntry).where(
            models.SectionRegistryEntry.registry_version_id == context["registry_version_id"]
        )))
        old_snapshot = [(x.section_id, x.ordinal, x.display_name, x.workflow_role) for x in old_entries]
        old_alias = session.scalar(select(models.SectionExternalAlias).where(
            models.SectionExternalAlias.section_id == context["section_id"],
            models.SectionExternalAlias.external_id == "1217084805070731",
        ))
        assert old_alias is not None and old_alias.state == "active"
        state = session.get(
            models.DishState, (context["generation_id"], _task_id)
        )
        assert state is not None
        dish_version = state.dish_version
        placement_version = state.placement_version
        result = _call(session, ids, context)
        assert result["changed"] is True
        assert result["before"] == {"registry_version_id": str(context["registry_version_id"]), "registry_revision": 1}
        assert result["after"]["registry_version_id"] != str(context["registry_version_id"])
        assert result["after"]["registry_revision"] == 2
        sections = list(PostgresReadModel(session, cursor_secret=SECRET).sections())
        assert [x["name"] for x in sections] == list(NAMES)
        assert [x["section_gid"] for x in sections] == list(GIDS)
        assert [x["workflow_role"] for x in sections] == [
            "research_queue", "verification_queue", f"imported-section-{GIDS[2]}", f"imported-section-{GIDS[3]}"
        ]
        historical = tuple(session.scalars(select(models.SectionRegistryEntry).where(
            models.SectionRegistryEntry.registry_version_id == context["registry_version_id"]
        )))
        assert [(x.section_id, x.ordinal, x.display_name, x.workflow_role) for x in historical] == old_snapshot

        session.refresh(old_alias)
        assert old_alias.state == "retired"
        assert old_alias.retired_at is not None
        assert old_alias.retired_at.replace(tzinfo=NOW.tzinfo) == NOW
        replacement_alias = session.scalar(select(models.SectionExternalAlias).where(
            models.SectionExternalAlias.external_id == GIDS[0],
            models.SectionExternalAlias.state == "active",
        ))
        assert replacement_alias is not None
        assert replacement_alias.section_id == context["section_id"]

        session.refresh(state)
        assert state.section_id == context["section_id"]
        assert state.registry_version_id == uuid.UUID(result["after"]["registry_version_id"])
        assert state.dish_version == dish_version + 1
        assert state.placement_version == state.dish_version
        assert state.placement_version != placement_version
        receipt = session.get(
            models.DishMutationReceipt,
            (context["generation_id"], _task_id, state.dish_version),
        )
        assert receipt is not None
        assert receipt.placement_changed is True
        assert receipt.content_changed is False
        assert receipt.completion_changed is False
        assert receipt.source_route == "import"
        assert receipt.import_run_id == uuid.UUID(result["import_run_id"])
        task_page = PostgresReadModel(session, cursor_secret=SECRET).section_tasks(
            section_reference=GIDS[0]
        )
        assert [item.task_id for item in task_page.items] == [_task_id]

        authority = {"ok": True, "command": "sections", "data": {"sections": sections}}
        legacy = {"ok": True, "command": "sections", "data": {"sections": [
            {"gid": gid, "name": name} for gid, name in zip(GIDS, NAMES, strict=True)
        ]}}
        scenario = next(x for x in comparator.load_plan(PLAN)["scenarios"] if x["id"] == "sections")
        drop_keys = frozenset(scenario["compare"]["drop_keys"])
        assert comparator.normalize_value(authority, drop_keys=drop_keys) == comparator.normalize_value(legacy, drop_keys=drop_keys)


def test_test_membership_revision_refuses_true_current_membership_removal(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        section = session.get(models.GovernedSection, context["section_id"])
        assert section is not None
        section.logical_name = "Unexpected Existing Section"
        session.flush()
        before = _counts(session)
        with pytest.raises(
            ReviseTestSectionRegistryMembershipError,
            match="membership revision refuses to remove existing registry sections",
        ):
            _call(session, ids, context)
        assert _counts(session) == before
        assert _active(session, context["generation_id"]) == (context["registry_version_id"], 1)

def test_test_membership_revision_stale_generation_refuses_without_mutation(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        before = _counts(session)
        with pytest.raises(ReviseTestSectionRegistryMembershipError, match="stale active generation"):
            _call(session, ids, context, expected_generation_id=uuid.uuid4())
        assert _counts(session) == before
        assert _active(session, context["generation_id"]) == (context["registry_version_id"], 1)


@pytest.mark.parametrize("field", ["version", "revision"])
def test_test_membership_revision_stale_registry_refuses_without_mutation(workflow_db, field) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        before = _counts(session)
        changes = {"expected_registry_version_id": uuid.uuid4()} if field == "version" else {"expected_registry_revision": 2}
        with pytest.raises(ReviseTestSectionRegistryMembershipError, match="stale active registry"):
            _call(session, ids, context, **changes)
        assert _counts(session) == before
        assert _active(session, context["generation_id"]) == (context["registry_version_id"], 1)


def test_test_membership_revision_exact_retry_is_safe(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        first = _call(session, ids, context)
        after_first = _counts(session)
        retry = _call(session, ids, context)
        assert retry["changed"] is False and retry["already_applied"] is True
        assert retry["import_run_id"] == first["import_run_id"]
        assert retry["service_run_id"] == first["service_run_id"]
        assert retry["after"] == first["after"]
        assert _counts(session) == after_first


def test_test_membership_revision_primitive_refuses_non_postgresql_session_before_mutation(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        before = _counts(session)
        with pytest.raises(ReviseTestSectionRegistryMembershipError, match="requires PostgreSQL"):
            revise_test_section_registry_membership(
                session,
                target_database_name=TEST_DATABASE_NAME,
                expected_generation_id=context["generation_id"],
                expected_registry_version_id=context["registry_version_id"],
                expected_registry_revision=1,
                research_queue_section_gid=GIDS[0],
                verification_queue_section_gid=GIDS[1],
                sourcing_section_gid=GIDS[2],
                reference_section_gid=GIDS[3],
                owner_id="Marco",
                agent="marco",
                now=NOW,
                uuid_factory=lambda: next(ids),
            )
        assert _counts(session) == before
        assert _active(session, context["generation_id"]) == (context["registry_version_id"], 1)


def test_test_membership_revision_primitive_refuses_connected_non_test_postgresql_before_mutation() -> None:
    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _Session:
        bind = _Bind()

        def __init__(self) -> None:
            self.scalar_calls = 0

        def scalar(self, statement):
            self.scalar_calls += 1
            assert str(statement) == "SELECT current_database()"
            return "dish_stage_a_prod"

        def __getattr__(self, name):
            raise AssertionError(f"mutation access after target fence: {name}")

    session = _Session()
    with pytest.raises(
        ReviseTestSectionRegistryMembershipError,
        match="connected database must be exact TEST database",
    ):
        revise_test_section_registry_membership(
            session,
            target_database_name=TEST_DATABASE_NAME,
            expected_generation_id=uuid.uuid4(),
            expected_registry_version_id=uuid.uuid4(),
            expected_registry_revision=1,
            research_queue_section_gid=GIDS[0],
            verification_queue_section_gid=GIDS[1],
            sourcing_section_gid=GIDS[2],
            reference_section_gid=GIDS[3],
            owner_id="Marco",
            agent="marco",
            now=NOW,
        )
    assert session.scalar_calls == 1


def test_test_membership_revision_refuses_non_test_targets(workflow_db) -> None:
    for url, match in (
        ("postgresql+psycopg://dish@localhost/dish_stage_a_dev", "exact TEST database"),
        ("postgresql+psycopg://dish@localhost/dish_stage_a_prod", "containing 'prod'"),
        ("sqlite+pysqlite:///:memory:", "requires PostgreSQL"),
    ):
        with pytest.raises(ReviseTestSectionRegistryMembershipError, match=match):
            require_test_database_url(url, expected_database_name=TEST_DATABASE_NAME)
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        before = _counts(session)
        with pytest.raises(ReviseTestSectionRegistryMembershipError, match="exact TEST target"):
            _call(session, ids, context, target_database_name="dish_stage_a_prod")
        assert _counts(session) == before


def test_existing_role_only_correction_still_cannot_change_membership(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        verification_id = next(ids)
        session.add(models.GovernedSection(
            section_id=verification_id, project_id=context["project_id"], logical_name="Verification Queue",
            lifecycle="active", import_run_id=context["import_run_id"], created_at=NOW, retired_at=None,
        ))
        session.add(models.SectionExternalAlias(
            alias_id=next(ids), section_id=verification_id, external_system="asana", external_id=GIDS[1],
            origin="imported", import_run_id=context["import_run_id"], projection_event_id=None,
            state="active", created_at=NOW, retired_at=None,
        ))
        session.flush()
        result = revise_section_registry(
            session, research_queue_section_id=context["section_id"], verification_queue_section_id=verification_id,
            owner_id="Marco", agent="marco", cursor_secret=SECRET, now=NOW, uuid_factory=lambda: next(ids),
        )
        assert result.ok is False and result.code == "REGISTRY_SECTION_MISSING"
        assert _active(session, context["generation_id"]) == (context["registry_version_id"], 1)
        assert len(PostgresReadModel(session, cursor_secret=SECRET).sections()) == 1


def test_revise_section_registry_assigns_both_roles(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        verification_section_id = _add_destination_section(session, ids, context, external_id="1217084805070799")
        result = revise_section_registry(
            session, research_queue_section_id=context["section_id"], verification_queue_section_id=verification_section_id,
            owner_id="Marco", agent="marco", cursor_secret=SECRET, now=NOW, uuid_factory=lambda: next(ids),
        )
        assert result.ok is True, (result.code, result.http_status, result.data)
        assert result.data["changed"] is True


def test_revise_section_registry_rejects_identical_sections(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        with pytest.raises(ReviseSectionRegistryError):
            revise_section_registry(
                session, research_queue_section_id=context["section_id"], verification_queue_section_id=context["section_id"],
                owner_id="Marco", agent="marco", cursor_secret=SECRET, now=NOW, uuid_factory=lambda: next(ids),
            )


def test_revise_section_registry_is_idempotent_on_rerun(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        verification_section_id = _add_destination_section(session, ids, context, external_id="1217084805070799")
        kwargs = dict(
            research_queue_section_id=context["section_id"], verification_queue_section_id=verification_section_id,
            owner_id="Marco", agent="marco", cursor_secret=SECRET, now=NOW, uuid_factory=lambda: next(ids),
        )
        assert revise_section_registry(session, **kwargs).ok is True
        second = revise_section_registry(session, **kwargs)
        assert second.ok is True and second.data["changed"] is False
