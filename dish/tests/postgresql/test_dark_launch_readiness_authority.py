from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg.bootstrap import (
    DEFAULT_PROJECT_GID,
    DEFAULT_PROJECT_ID,
    DEFAULT_SCHEMA_HEAD,
    HonestCheckout,
    InitialBootstrapSpec,
    bootstrap_initial_generation,
    inspect_source_bundle,
    section_specs_from_bundle,
)
from dish_pg.dark_launch_readiness import (
    ArtifactBinding,
    DarkLaunchReadinessError,
    _authority_checks,
    bind_artifacts,
    inspect_postgresql_read_only,
)
from dish_pg.database import session_scope
from dish_pg.import_runtime import already_imported, prepare_import_run
from dish_pg.importer import run_import
from dish_pg.transition import ProjectionService, ShadowService
from tests.support.postgresql.core import HASH_A, HASH_B, NOW
from tests.support.postgresql.dark_launch_readiness import record


DEFAULT_SECTION_GID = "1216891250619908"
DEFAULT_SECTION_ID = uuid.UUID("8b5bfb31-b986-5116-a207-569a5ba95907")


@dataclass
class _AuthorityFixture:
    engine: Any
    factory: Any
    manifest: Path
    ndjson: Path
    receipt_path: Path
    receipt: dict[str, object]
    binding: ArtifactBinding


def _artifact_files(tmp_path: Path, *, task_id: uuid.UUID):
    task_record = record(task_id, DEFAULT_PROJECT_ID, DEFAULT_SECTION_ID, DEFAULT_SECTION_GID)
    ndjson = tmp_path / "legacy.ndjson"
    ndjson.write_text(json.dumps(task_record, sort_keys=True) + "\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "tasks": {
                    "123456789": {
                        key: task_record[key]
                        for key in (
                            "task_id",
                            "project_ids",
                            "section_id",
                            "section_gid",
                            "completed",
                            "observed_at",
                            "existence_state",
                        )
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    source = inspect_source_bundle(
        ndjson,
        project_id=DEFAULT_PROJECT_ID,
    )
    honest = HonestCheckout(
        root=tmp_path,
        commit="f" * 40,
        protocol_version="1.0.10",
        schema_version="2",
        protocol_sha256=HASH_A,
        schema_sha256=HASH_B,
        protocol_files={"planning": {"path": "planning.md", "sha256": HASH_A}},
    )
    spec = InitialBootstrapSpec(
        dish_commit="42619b9",
        schema_head=DEFAULT_SCHEMA_HEAD,
        source_generation="legacy-1",
        source_bundle=source,
        honest=honest,
        project_id=DEFAULT_PROJECT_ID,
        project_gid=DEFAULT_PROJECT_GID,
        project_name="Cooking",
        sections=section_specs_from_bundle(source),
    )
    return manifest, ndjson, spec


def _build_authority_fixture(tmp_path: Path) -> _AuthorityFixture:
    baseline_id = uuid.uuid4()
    manifest, ndjson, spec = _artifact_files(tmp_path, task_id=uuid.uuid4())
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'authority.sqlite3'}", future=True)
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    with session_scope(factory) as session:
        result = bootstrap_initial_generation(
            session,
            spec,
            uuid_factory=lambda: uuid.uuid4(),
            clock=lambda: NOW,
        )
    summary = run_import(
        source=ndjson,
        generation_id=result.generation_id,
        import_run_id=result.import_run_id,
        contract_binding_id=result.binding_id,
        session_factory=factory,
        prepare_import_run=prepare_import_run,
        already_imported=already_imported,
    )
    if (summary.imported, summary.skipped, summary.failed) != (1, 0, 0):
        raise AssertionError(f"unexpected import summary: {summary}")
    with session_scope(factory) as session:
        ShadowService(session, uuid_factory=lambda: baseline_id).create_baseline(
            generation_id=result.generation_id,
            source_generation_identity=spec.source_generation,
            source_commit=spec.dish_commit,
            created_at=NOW,
        )
        ProjectionService(session, uuid_factory=lambda: uuid.uuid4()).activate_epoch(
            generation_id=result.generation_id,
            activation_reason="readiness fixture",
            created_at=NOW,
            external_effects_enabled=False,
        )

    receipt = result.as_json()
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    for path in (ndjson, manifest, receipt_path):
        path.chmod(0o600)
    binding = bind_artifacts(
        manifest_path=manifest,
        ndjson_path=ndjson,
        receipt_path=receipt_path,
        baseline_id=baseline_id,
    )
    return _AuthorityFixture(
        engine=engine,
        factory=factory,
        manifest=manifest,
        ndjson=ndjson,
        receipt_path=receipt_path,
        receipt=receipt,
        binding=binding,
    )


def _assert_artifact_drift_is_rejected(state: _AuthorityFixture) -> None:
    stale_receipt = dict(state.receipt)
    stale_receipt["schema_head"] = "stale-schema-head"
    state.receipt_path.write_text(json.dumps(stale_receipt), encoding="utf-8")
    with pytest.raises(DarkLaunchReadinessError, match="expected Alembic head"):
        bind_artifacts(
            manifest_path=state.manifest,
            ndjson_path=state.ndjson,
            receipt_path=state.receipt_path,
            baseline_id=state.binding.baseline_id,
        )
    state.receipt_path.write_text(json.dumps(state.receipt), encoding="utf-8")

    value = json.loads(state.manifest.read_text(encoding="utf-8"))
    value["tasks"]["123456789"]["section_id"] = str(uuid.uuid4())
    state.manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(DarkLaunchReadinessError, match="manifest and NDJSON"):
        bind_artifacts(
            manifest_path=state.manifest,
            ndjson_path=state.ndjson,
            receipt_path=state.receipt_path,
            baseline_id=state.binding.baseline_id,
        )


def _assert_database_drift_is_reported(state: _AuthorityFixture) -> None:
    with session_scope(state.factory) as session:
        import_run = session.get(models.ImportRun, state.binding.import_run_id)
        if import_run is None:
            raise AssertionError("fixture import run missing")
        import_run.source_commit = "wrong-source-commit"
    with session_scope(state.factory) as session:
        source_checks = _authority_checks(session, binding=state.binding)
    assert source_checks["source_import"]["passed"] is False
    with session_scope(state.factory) as session:
        import_run = session.get(models.ImportRun, state.binding.import_run_id)
        if import_run is None:
            raise AssertionError("fixture import run missing")
        import_run.source_commit = state.binding.source_commit

    with session_scope(state.factory) as session:
        epoch = session.scalar(select(tx.ProjectionEpoch))
        if epoch is None:
            raise AssertionError("fixture projection epoch missing")
        epoch.external_effects_enabled = True
    with session_scope(state.factory) as session:
        unsafe_checks = _authority_checks(session, binding=state.binding)
    assert unsafe_checks["projection_epoch"]["passed"] is False


def test_artifact_and_database_authority_bindings_are_exact(tmp_path: Path) -> None:
    state = _build_authority_fixture(tmp_path)
    try:
        with session_scope(state.factory) as session:
            checks = _authority_checks(session, binding=state.binding)
        assert checks and all(item["passed"] for item in checks.values()), checks
        _assert_database_drift_is_reported(state)
        _assert_artifact_drift_is_rejected(state)
    finally:
        state.engine.dispose()


class _FakeTransaction:
    def __init__(self) -> None:
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True


class _FakeScalars:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._values)


class _FakeConnection:
    def __init__(self) -> None:
        self.transaction = _FakeTransaction()
        self.commands: list[str] = []

    def execution_options(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def begin(self):
        return self.transaction

    def exec_driver_sql(self, command: str):
        self.commands.append(command)

    def scalar(self, _statement):
        return "dish_prod"

    def execute(self, _statement):
        return _FakeScalars([DEFAULT_SCHEMA_HEAD])


class _FakeEngine:
    def __init__(self) -> None:
        self.dialect = SimpleNamespace(name="postgresql")
        self.url = "postgresql+psycopg://dish:secret@localhost/dish_prod"
        self.connection = _FakeConnection()

    def connect(self):
        return self.connection


def test_postgresql_preflight_sets_read_only_and_always_rolls_back(monkeypatch) -> None:
    engine = _FakeEngine()
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.Session",
        lambda **_kwargs: SimpleNamespace(close=lambda: None),
    )
    binding = ArtifactBinding(
        receipt={},
        records=(),
        source_sha256="a" * 64,
        source_count=0,
        manifest_count=0,
        generation_id=uuid.uuid4(),
        import_run_id=uuid.uuid4(),
        binding_id=uuid.uuid4(),
        baseline_id=uuid.uuid4(),
        source_generation="legacy",
        source_commit="commit",
    )
    checks = inspect_postgresql_read_only(
        engine=engine,
        expected_database_name="dish_prod",
        expected_schema_head=DEFAULT_SCHEMA_HEAD,
        binding=binding,
        authority_reader=lambda _session: {
            "authority": {"passed": True, "status": "pass", "reason": "fixture"}
        },
    )
    assert checks["database_identity"]["passed"] is True
    assert engine.connection.commands == ["SET TRANSACTION READ ONLY"]
    assert engine.connection.transaction.rolled_back is True
