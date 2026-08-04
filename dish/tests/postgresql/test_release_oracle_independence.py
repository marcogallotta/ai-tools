"""Mutation tests for release-contract inventories and canonical hashes."""
from __future__ import annotations

import pytest

from dish_pg.database import session_scope
from dish_pg.release import (
    EVIDENCE_ARTIFACT_KINDS,
    REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    REQUIRED_EVIDENCE,
    REQUIRED_REHEARSALS,
    REQUIRED_REHEARSAL_CHECKPOINTS,
    ReleaseAuthorityError,
    canonical_json,
    sha256_json,
)
from tests.support.postgresql.release import HASH_A, _prepare_candidate
from tests.support.postgresql.release_oracles import (
    CANONICAL_VECTOR_BYTES,
    CANONICAL_VECTOR_SHA256,
    CANONICAL_VECTOR_VALUE,
    EXPECTED_EVIDENCE_ARTIFACT_KINDS,
    EXPECTED_RELEASE_EVIDENCE,
    EXPECTED_REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    EXPECTED_REHEARSAL_CHECKPOINTS,
    EXPECTED_REHEARSALS,
    assert_exact_inventory,
    assert_exact_mapping,
    independent_canonical_json,
    independent_sha256_json,
)
from tests.support.postgresql.workflow import NOW, workflow_db

pytestmark = pytest.mark.smoke


def test_literal_release_oracles_match_production_contracts_exactly() -> None:
    assert_exact_inventory(REQUIRED_EVIDENCE, EXPECTED_RELEASE_EVIDENCE, label="release evidence")
    assert_exact_mapping(
        EVIDENCE_ARTIFACT_KINDS,
        EXPECTED_EVIDENCE_ARTIFACT_KINDS,
        label="evidence artifact kinds",
    )
    assert_exact_inventory(REQUIRED_REHEARSALS, EXPECTED_REHEARSALS, label="rehearsals")
    assert_exact_mapping(
        REQUIRED_REHEARSAL_CHECKPOINTS,
        EXPECTED_REHEARSAL_CHECKPOINTS,
        label="rehearsal checkpoints",
    )
    for kind in EXPECTED_REHEARSALS:
        assert_exact_mapping(
            REHEARSAL_CHECKPOINT_EVIDENCE_KINDS[kind],
            EXPECTED_REHEARSAL_CHECKPOINT_EVIDENCE_KINDS[kind],
            label=f"{kind} checkpoint evidence kinds",
        )


def test_canonical_hash_vector_is_precomputed_independently() -> None:
    assert independent_canonical_json(CANONICAL_VECTOR_VALUE) == CANONICAL_VECTOR_BYTES
    assert independent_sha256_json(CANONICAL_VECTOR_VALUE) == CANONICAL_VECTOR_SHA256
    assert canonical_json(CANONICAL_VECTOR_VALUE) == CANONICAL_VECTOR_BYTES
    assert sha256_json(CANONICAL_VECTOR_VALUE) == CANONICAL_VECTOR_SHA256


def test_same_count_wrong_identity_and_weakened_inventories_are_rejected() -> None:
    wrong_identity = EXPECTED_RELEASE_EVIDENCE[:-1] + (("protocol_coherence", "wrong_key"),)
    with pytest.raises(AssertionError, match="missing=.*service_openapi_routing"):
        assert_exact_inventory(wrong_identity, EXPECTED_RELEASE_EVIDENCE, label="release evidence")
    with pytest.raises(AssertionError, match="actual_count=0"):
        assert_exact_inventory((), EXPECTED_RELEASE_EVIDENCE, label="release evidence")
    with pytest.raises(AssertionError, match="actual_count=8"):
        assert_exact_inventory(
            EXPECTED_RELEASE_EVIDENCE[:-1],
            EXPECTED_RELEASE_EVIDENCE,
            label="release evidence",
        )


def test_candidate_missing_one_literal_required_evidence_fails_evaluation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    omitted = EXPECTED_RELEASE_EVIDENCE[-1]
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(
            session,
            ids,
            context,
            task_id,
            evidence_contracts=EXPECTED_RELEASE_EVIDENCE[:-1],
        )
        evaluation = service.evaluate_candidate(candidate_id=candidate_id)
        check = next(item for item in evaluation.checks if item.code == "required_acceptance_evidence")
        assert not check.passed
        assert check.details["evidence"][":".join(omitted)] is None


def test_unknown_replacement_evidence_is_rejected_even_at_same_count(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        with pytest.raises(ReleaseAuthorityError, match="unknown release evidence contract"):
            service.record_evidence(
                candidate_id=candidate_id,
                category="protocol_coherence",
                evidence_key="wrong_key",
                outcome="pass",
                payload={
                    "artifact_kind": "protocol-coherence-report",
                    "artifact_identity": "fixture:wrong-identity",
                    "artifact_path": "/evidence/protocol_coherence/wrong.json",
                    "artifact_sha256": HASH_A,
                    "source_manifest_sha256": HASH_A,
                    "gate_name": "protocol_coherence:wrong_key",
                    "gate_result": "pass",
                },
                recorded_at=NOW,
            )
