from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from sqlalchemy import select

from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import ReleaseAuthorityError
from tests.support.postgresql.workflow import NOW, workflow_db
from tests.support.postgresql.release import HASH_A, ROOT, _prepare_candidate
from tests.support.postgresql.release_oracles import (
    EXPECTED_EVIDENCE_ARTIFACT_KINDS,
    independent_sha256_json,
)

pytestmark = pytest.mark.smoke


def _valid_evidence_payload(category: str, evidence_key: str) -> dict[str, object]:
    return {
        "artifact_kind": EXPECTED_EVIDENCE_ARTIFACT_KINDS[(category, evidence_key)],
        "artifact_identity": f"fixture:{category}:{evidence_key}:replacement",
        "artifact_path": f"/evidence/{category}/{evidence_key}.json",
        "artifact_sha256": "b" * 64,
        "source_manifest_sha256": HASH_A,
        "gate_name": f"{category}:{evidence_key}",
        "gate_result": "pass",
    }


def test_release_evidence_rejects_bare_self_attestation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        with pytest.raises(ReleaseAuthorityError, match="artifact kind"):
            service.record_evidence(
                candidate_id=candidate_id,
                category="authority_coverage",
                evidence_key="current_to_target",
                outcome="pass",
                payload={"result": "pass"},
                recorded_at=NOW,
            )


def test_release_evidence_requires_exact_lowercase_sha256(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        payload = _valid_evidence_payload("authority_coverage", "current_to_target")
        payload["artifact_sha256"] = "Z" * 64
        with pytest.raises(ReleaseAuthorityError, match="lowercase hexadecimal SHA-256"):
            service.record_evidence(
                candidate_id=candidate_id,
                category="authority_coverage",
                evidence_key="current_to_target",
                outcome="pass",
                payload=payload,
                recorded_at=NOW,
            )


def test_rehearsal_report_binds_exact_checkpoint_manifest(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        rehearsal = session.scalar(
            select(rel.RehearsalRun).where(
                rel.RehearsalRun.candidate_id == candidate_id,
                rel.RehearsalRun.rehearsal_kind == "full",
            )
        )
        assert rehearsal is not None
        with pytest.raises(ReleaseAuthorityError, match="exact checkpoint set"):
            service.finish_rehearsal(
                rehearsal_id=rehearsal.rehearsal_id,
                passed=True,
                report={
                    "rehearsal_kind": "full",
                    "source_manifest_sha256": HASH_A,
                    "result": "passed",
                    "checkpoint_manifest_sha256": "f" * 64,
                },
                completed_at=NOW,
            )


def test_final_asana_capture_manifest_uses_same_sha256_contract(workflow_db) -> None:
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
            validated_at=NOW,
        )
        with pytest.raises(ReleaseAuthorityError, match="capture_manifest_sha256"):
            service.record_final_asana_closure(
                candidate_id=candidate_id,
                capture_manifest_sha256="z" * 64,
                observation_high_water="asana-change-1",
                watcher_identity="watcher@fixture",
                interval_started_at=NOW,
                closed_through_at=NOW,
                payload={"registry": "closed"},
                recorded_at=NOW,
            )


def test_operator_json_rejects_duplicate_keys_recursively(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-release"))
    load = namespace["_load"]
    payload = tmp_path / "evidence.json"
    payload.write_text(
        '{"artifact":{"sha256":"' + HASH_A + '","sha256":"' + ("b" * 64) + '"}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON object key: sha256"):
        load(payload)


def test_valid_typed_evidence_remains_deterministic(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        payload = _valid_evidence_payload("authority_coverage", "current_to_target")
        row = service.record_evidence(
            candidate_id=candidate_id,
            category="authority_coverage",
            evidence_key="current_to_target",
            outcome="pass",
            payload=payload,
            recorded_at=NOW,
        )
        assert row.payload_sha256 == independent_sha256_json(payload)
