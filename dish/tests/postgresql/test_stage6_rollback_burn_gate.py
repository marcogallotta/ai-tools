from __future__ import annotations
from datetime import timedelta
import json
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import ALEMBIC_HEAD, ReleaseAuthorityError, ReleaseCandidateService
from dish_pg.release_status import AcceptanceCheck, CandidateEvaluation
from dish_pg.workflow import (
    ExecutionSpec,
    MutationAdmissionClosed,
    RequestSpec,
    StoredOutcome,
    WorkflowAuthorityService,
    sha256_json,
)
from tests.support.postgresql.first_admission import (
    _prepare_approved_cutover,
    _activate_authority,
    _assert_admission_closed,
    _burn_and_open_admission,
    _record_committed_first_request,
    _verify_and_complete,
    open_verified_first_admission,
)
from tests.support.postgresql.release import (
    HASH_A,
    _complete_active_mapping_reconciliation,
    _prepare_candidate,
    _record_and_engage_writer_fence,
    _record_final_closure,
    _record_runtime_and_typed_readiness,
    _writer_fence_proof,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db



def test_rollback_burn_rechecks_candidate_quiescence_immediately_before_burn(
    workflow_db, monkeypatch
) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)

    calls: list[tuple[object, object]] = []
    with session_scope(factory) as session:
        ordering: list[str] = []
        service = ReleaseCandidateService(
            session,
            uuid_factory=lambda: _next(ids),
            rollback_burn_fence_hook=lambda: ordering.append("fence"),
        )

        def failed_evaluation(*, candidate_id, as_of):
            ordering.append("evaluate")
            calls.append((candidate_id, as_of))
            return CandidateEvaluation(
                candidate_id=candidate_id,
                checks=(
                    AcceptanceCheck(
                        "quiescent_cutover_authority",
                        False,
                        {"authority_operations": 1},
                    ),
                ),
            )

        monkeypatch.setattr(service, "evaluate_candidate", failed_evaluation)
        with pytest.raises(
            ReleaseAuthorityError,
            match="failed immediately before rollback burn: quiescent_cutover_authority",
        ):
            service.burn_rollback(
                cutover_run_id=cutover_id,
                legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
                burned_at=NOW + timedelta(minutes=6),
            )
        assert ordering == ["fence", "evaluate"]
        assert calls == [(candidate_id, NOW + timedelta(minutes=6))]
        activation = session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"],
                models.AuthorityActivation.outcome == "activated",
            )
        )
        assert activation is None
