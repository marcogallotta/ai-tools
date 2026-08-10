"""PGlite coverage for persisted shadow-delivery authority predicates."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dish_pg.transition import ShadowService, TransitionAuthorityError
from tests.support.postgresql.core import ROOT, _bootstrap_registry, _import_one, _uuid_stream
from tests.support.postgresql.workflow import NOW, _next

pytestmark = pytest.mark.pglite


def test_pglite_shadow_settlement_rejects_superseded_claim(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", pglite.sqlalchemy_url)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
            ids = _uuid_stream()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    context = _bootstrap_registry(session, ids, generation_status="active")
                    _import_one(session, ids, context)
                    service = ShadowService(session, uuid_factory=lambda: _next(ids))
                    baseline = service.create_baseline(
                        generation_id=context["generation_id"],
                        source_generation_identity="legacy-1",
                        source_commit="pglite-shadow-authority",
                        created_at=NOW,
                    )
                    envelope = service.capture_envelope(
                        shadow_baseline_id=baseline.shadow_baseline_id,
                        command_name="prepare",
                        source_request_identity="pglite-superseded",
                        canonical_input={"command": "prepare", "arguments": {}},
                        source_outcome={"ok": True},
                        source_post_state={},
                        rollout_sequence=1,
                        source_authority_generation="legacy-1",
                        captured_at=NOW,
                    )
                    stale_token = uuid.uuid4()
                    stale_claim = service.claim_delivery(
                        worker_id="worker-1",
                        claim_token=stale_token,
                        now=NOW,
                        ttl=timedelta(minutes=1),
                        shadow_baseline_id=baseline.shadow_baseline_id,
                    )
                    current_token = uuid.uuid4()
                    current_claim = service.claim_delivery(
                        worker_id="worker-2",
                        claim_token=current_token,
                        now=NOW + timedelta(minutes=2),
                        ttl=timedelta(minutes=1),
                        shadow_baseline_id=baseline.shadow_baseline_id,
                    )
                    assert stale_claim is not None and current_claim is not None
                    with pytest.raises(TransitionAuthorityError, match="stale or expired"):
                        service.compare_delivery(
                            delivery_id=stale_claim.delivery_id,
                            claim_token=stale_token,
                            claim_revision=stale_claim.delivery_revision,
                            worker_id="worker-1",
                            target_result=dict(envelope.source_outcome),
                            comparator_release="pglite",
                            compared_at=NOW + timedelta(minutes=2, seconds=1),
                        )
                    comparison = service.compare_delivery(
                        delivery_id=current_claim.delivery_id,
                        claim_token=current_token,
                        claim_revision=current_claim.delivery_revision,
                        worker_id="worker-2",
                        target_result=dict(envelope.source_outcome),
                        comparator_release="pglite",
                        compared_at=NOW + timedelta(minutes=2, seconds=1),
                    )
                    assert comparison.parity_class == "exact"
    finally:
        engine.dispose()


def test_pglite_manual_recovery_requires_exact_failed_revision_and_proof(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", pglite.sqlalchemy_url)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
            ids = _uuid_stream()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    context = _bootstrap_registry(session, ids, generation_status="active")
                    _import_one(session, ids, context)
                    service = ShadowService(session, uuid_factory=lambda: _next(ids))
                    baseline = service.create_baseline(
                        generation_id=context["generation_id"],
                        source_generation_identity="legacy-1",
                        source_commit="pglite-shadow-recovery",
                        created_at=NOW,
                    )
                    envelope = service.capture_envelope(
                        shadow_baseline_id=baseline.shadow_baseline_id,
                        command_name="prepare",
                        source_request_identity="pglite-manual-recovery",
                        canonical_input={"command": "prepare", "arguments": {}},
                        source_outcome={"ok": True},
                        source_post_state={},
                        rollout_sequence=1,
                        source_authority_generation="legacy-1",
                        captured_at=NOW,
                    )
                    token = uuid.uuid4()
                    claim = service.claim_delivery(
                        worker_id="worker-1",
                        claim_token=token,
                        now=NOW,
                        ttl=timedelta(minutes=1),
                        shadow_baseline_id=baseline.shadow_baseline_id,
                    )
                    assert claim is not None
                    gap = service.fail_delivery(
                        delivery_id=claim.delivery_id,
                        claim_token=token,
                        claim_revision=claim.delivery_revision,
                        worker_id="worker-1",
                        error="unknown commit outcome",
                        failed_at=NOW + timedelta(seconds=1),
                    )
                    with pytest.raises(TransitionAuthorityError, match="remains uncertain"):
                        service.resolve_gap(
                            gap_id=gap.gap_id,
                            resolution={"delivery_outcome": "uncertain"},
                            resolved_at=NOW + timedelta(seconds=2),
                        )
                    resolved = service.resolve_gap(
                        gap_id=gap.gap_id,
                        resolution={
                            "delivery_outcome": "not_applied",
                            "evidence": "request journal rollback proof",
                        },
                        resolved_at=NOW + timedelta(seconds=3),
                    )
                    session.refresh(claim)
                    assert resolved.state == "resolved"
                    assert claim.envelope_id == envelope.envelope_id
                    assert claim.state == "pending"
                    assert claim.delivery_revision == gap.details["failed_delivery_revision"] + 1
    finally:
        engine.dispose()


def test_pglite_failed_delivery_does_not_block_later_rollout_or_allow_post_evaluation_requeue(
    pglite,
) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", pglite.sqlalchemy_url)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
            ids = _uuid_stream()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    context = _bootstrap_registry(session, ids, generation_status="active")
                    _import_one(session, ids, context)
                    service = ShadowService(session, uuid_factory=lambda: _next(ids))
                    baseline = service.create_baseline(
                        generation_id=context["generation_id"],
                        source_generation_identity="legacy-1",
                        source_commit="pglite-shadow-failure-isolation",
                        created_at=NOW,
                    )
                    first = service.capture_envelope(
                        shadow_baseline_id=baseline.shadow_baseline_id,
                        command_name="prepare",
                        source_request_identity="pglite-failed-first",
                        canonical_input={"command": "prepare", "arguments": {}},
                        source_outcome={"ok": True},
                        source_post_state={},
                        rollout_sequence=1,
                        source_authority_generation="legacy-1",
                        captured_at=NOW,
                    )
                    second = service.capture_envelope(
                        shadow_baseline_id=baseline.shadow_baseline_id,
                        command_name="prepare",
                        source_request_identity="pglite-later-second",
                        canonical_input={"command": "prepare", "arguments": {}},
                        source_outcome={"ok": True},
                        source_post_state={},
                        rollout_sequence=2,
                        source_authority_generation="legacy-1",
                        captured_at=NOW + timedelta(seconds=1),
                    )
                    first_token = uuid.uuid4()
                    first_claim = service.claim_delivery(
                        worker_id="worker-1",
                        claim_token=first_token,
                        now=NOW + timedelta(seconds=2),
                        ttl=timedelta(minutes=1),
                        shadow_baseline_id=baseline.shadow_baseline_id,
                    )
                    assert first_claim is not None
                    assert first_claim.envelope_id == first.envelope_id
                    gap = service.fail_delivery(
                        delivery_id=first_claim.delivery_id,
                        claim_token=first_token,
                        claim_revision=first_claim.delivery_revision,
                        worker_id="worker-1",
                        error="definite rollback",
                        failed_at=NOW + timedelta(seconds=3),
                    )

                    second_token = uuid.uuid4()
                    second_claim = service.claim_delivery(
                        worker_id="worker-2",
                        claim_token=second_token,
                        now=NOW + timedelta(seconds=4),
                        ttl=timedelta(minutes=1),
                        shadow_baseline_id=baseline.shadow_baseline_id,
                    )
                    assert second_claim is not None
                    assert second_claim.envelope_id == second.envelope_id
                    service.compare_delivery(
                        delivery_id=second_claim.delivery_id,
                        claim_token=second_token,
                        claim_revision=second_claim.delivery_revision,
                        worker_id="worker-2",
                        target_result=dict(second.source_outcome),
                        comparator_release="pglite",
                        compared_at=NOW + timedelta(seconds=5),
                    )

                    with pytest.raises(
                        TransitionAuthorityError,
                        match="later rollout is in flight or after later rollout evaluation",
                    ):
                        service.resolve_gap(
                            gap_id=gap.gap_id,
                            resolution={
                                "delivery_outcome": "not_applied",
                                "evidence": "request journal rollback proof",
                            },
                            resolved_at=NOW + timedelta(seconds=6),
                        )
    finally:
        engine.dispose()
