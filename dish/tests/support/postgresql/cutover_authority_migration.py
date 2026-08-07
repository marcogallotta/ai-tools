"""Portable populated-predecessor fixtures for cutover-authority migration tests."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Connection

from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from tests.support.postgresql.migrations import MigrationDatabase

PREDECESSOR_REVISION = "0028_consumed_first_request_open_admission"
TARGET_REVISION = "0030_validation_failure_admission"


@dataclass(frozen=True)
class CandidateDependencySeed:
    dependency_generation_id: uuid.UUID
    candidate_generation_id: uuid.UUID
    candidate_id: uuid.UUID


def seed_candidate_dependency_predecessor(
    database: MigrationDatabase,
    *,
    mismatched: bool,
) -> CandidateDependencySeed:
    dependency_generation_id = uuid.uuid4()
    candidate_generation_id = uuid.uuid4() if mismatched else dependency_generation_id
    import_run_id = uuid.uuid4()
    import_batch_id = uuid.uuid4()
    shadow_baseline_id = uuid.uuid4()
    projection_epoch_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    recorded_at = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)

    def seed(connection: Connection) -> CandidateDependencySeed:
        generation_rows = [
            {
                "generation_id": dependency_generation_id,
                "predecessor_generation_id": None,
                "creation_reason": "initial_cutover",
                "external_restore_control_id": None,
                "schema_head": PREDECESSOR_REVISION,
                "dish_release": "dish-cutover-migration-test",
                "status": "pending",
                "created_at": recorded_at,
                "retired_at": None,
            }
        ]
        if candidate_generation_id != dependency_generation_id:
            generation_rows.append(
                {
                    **generation_rows[0],
                    "generation_id": candidate_generation_id,
                }
            )
        connection.execute(models.AuthorityGeneration.__table__.insert(), generation_rows)
        connection.execute(
            models.ImportRun.__table__.insert(),
            {
                "import_run_id": import_run_id,
                "source_commit": "a" * 64,
                "source_release": "source-cutover-migration-test",
                "legacy_generation_id": "legacy-generation-cutover-test",
                "baseline_high_water_mark": "legacy-high-water-cutover-test",
                "source_bundle_sha256": "b" * 64,
                "status": "complete",
                "started_at": recorded_at,
                "completed_at": recorded_at,
                "provenance": {"fixture": "cutover-authority-migration"},
            },
        )
        connection.execute(
            tx.SourceImportBatch.__table__.insert(),
            {
                "import_batch_id": import_batch_id,
                "generation_id": dependency_generation_id,
                "import_run_id": import_run_id,
                "source_release": "source-cutover-migration-test",
                "source_commit": "a" * 64,
                "source_database_sha256": "c" * 64,
                "source_sidecars": {},
                "ledger_through_commit": "ledger-cutover-migration-test",
                "expected_entities": 0,
                "imported_entities": 0,
                "status": "complete",
                "started_at": recorded_at,
                "completed_at": recorded_at,
            },
        )
        connection.execute(
            tx.ShadowBaseline.__table__.insert(),
            {
                "shadow_baseline_id": shadow_baseline_id,
                "generation_id": dependency_generation_id,
                "source_generation_identity": "legacy-generation-cutover-test",
                "source_commit": "a" * 64,
                "baseline_sequence": 1,
                "status": "closed",
                "disqualification_reason": None,
                "created_at": recorded_at,
                "terminal_at": recorded_at,
            },
        )
        connection.execute(
            tx.ProjectionEpoch.__table__.insert(),
            {
                "projection_epoch_id": projection_epoch_id,
                "generation_id": dependency_generation_id,
                "epoch_number": 1,
                "status": "retired",
                "activation_reason": "cutover authority migration fixture",
                "external_effects_enabled": False,
                "created_at": recorded_at,
                "retired_at": recorded_at,
            },
        )
        connection.execute(
            rel.ReleaseCandidate.__table__.insert(),
            {
                "candidate_id": candidate_id,
                "generation_id": candidate_generation_id,
                "source_import_batch_id": import_batch_id,
                "shadow_baseline_id": shadow_baseline_id,
                "projection_epoch_id": projection_epoch_id,
                "source_release": "source-cutover-migration-test",
                "source_commit": "a" * 64,
                "ledger_through_commit": "ledger-cutover-migration-test",
                "schema_head": PREDECESSOR_REVISION,
                "dish_release": "dish-cutover-migration-test",
                "honest_release": "honest-cutover-migration-test",
                "protocol_release": "protocol-cutover-migration-test",
                "openapi_release": "openapi-cutover-migration-test",
                "routing_release": "routing-cutover-migration-test",
                "status": "assembling",
                "candidate_revision": 1,
                "validation_bundle_sha256": None,
                "created_at": recorded_at,
                "validated_at": None,
                "approved_at": None,
                "terminal_at": None,
            },
        )
        return CandidateDependencySeed(
            dependency_generation_id=dependency_generation_id,
            candidate_generation_id=candidate_generation_id,
            candidate_id=candidate_id,
        )

    return database.seed(seed)

def seed_unsafe_open_admission_predecessor(
    database: MigrationDatabase,
    *,
    seed: CandidateDependencySeed,
) -> None:
    recorded_at = datetime(2026, 8, 1, 20, 5, tzinfo=timezone.utc)

    def insert(connection: Connection) -> None:
        connection.execute(
            rel.MutationAdmissionControl.__table__.insert(),
            {
                "generation_id": seed.candidate_generation_id,
                "candidate_id": seed.candidate_id,
                "state": "open",
                "control_revision": 1,
                "opened_at": recorded_at,
                "updated_at": recorded_at,
            },
        )

    database.seed(insert)
