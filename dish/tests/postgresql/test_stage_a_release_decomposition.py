from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dish_pg import (
    cutover_chronology,
    cutover_control,
    final_asana_closure,
    release,
    release_evidence,
    release_status,
)


pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]


def test_release_facade_reexports_stable_authority_values() -> None:
    assert release.ReleaseAuthorityError is release_evidence.ReleaseAuthorityError
    assert release.ReleaseCandidateStatus is release_status.ReleaseCandidateStatus
    assert release.WriterFenceStatus is release_status.WriterFenceStatus
    assert release.AcceptanceCheck is release_status.AcceptanceCheck
    assert release.CandidateEvaluation is release_status.CandidateEvaluation
    assert release.sha256_json is release_evidence.sha256_json
    assert release.canonical_json is release_evidence.canonical_json
    assert release._require_at_or_after is cutover_chronology._require_at_or_after
    assert release._utc_comparable is cutover_chronology._utc_comparable
    assert issubclass(
        release.ReleaseCandidateService, final_asana_closure.FinalAsanaClosureAuthority
    )
    assert issubclass(
        release.ReleaseCandidateService, cutover_control.CutoverControlAuthority
    )


def test_release_service_module_does_not_redefine_extracted_authorities() -> None:
    source = (ROOT / "dish_pg" / "release.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    extracted = {
        "ReleaseAuthorityError",
        "ReleaseCandidateStatus",
        "WriterFenceStatus",
        "AcceptanceCheck",
        "CandidateEvaluation",
        "canonical_json",
        "sha256_json",
        "_require_at_or_after",
        "_utc_comparable",
        "_validate_evidence_payload",
        "_validate_checkpoint_payload",
    }
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }
    assert defined.isdisjoint(extracted)


def test_release_authority_modules_remain_transaction_free() -> None:
    for name in ("release_evidence.py", "release_status.py", "cutover_chronology.py"):
        source = (ROOT / "dish_pg" / name).read_text(encoding="utf-8")
        assert "sqlalchemy.orm" not in source
        assert "Session" not in source
        assert ".flush(" not in source
        assert ".commit(" not in source


def test_transactional_release_methods_are_owned_by_stable_authorities() -> None:
    final_methods = {
        "record_final_asana_closure",
        "invalidate_final_asana_closure",
        "recertify_candidate",
        "approve_candidate",
    }
    cutover_methods = {
        "prepare_writer_fence",
        "verify_writer_fence",
        "prepare_cutover",
        "activate_authority",
        "burn_rollback",
        "open_mutation_admission",
        "verify_first_admission",
    }
    assert final_methods.issubset(final_asana_closure.FinalAsanaClosureAuthority.__dict__)
    assert cutover_methods.issubset(cutover_control.CutoverControlAuthority.__dict__)
    assert final_methods.isdisjoint(release.ReleaseCandidateService.__dict__)
    assert cutover_methods.isdisjoint(release.ReleaseCandidateService.__dict__)
