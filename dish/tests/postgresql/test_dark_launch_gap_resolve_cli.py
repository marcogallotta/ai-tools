"""CLI wiring for operator recovery of a failed shadow delivery's gap."""
from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg.dark_launch import main
from dish_pg.database import session_scope
from dish_pg.transition import ShadowService, TransitionAuthorityError
from tests.support.postgresql.core import _bootstrap_registry, _uuid_stream
from tests.support.postgresql.workflow import NOW, _next


def _sqlite_engine(path: Path):
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    models.Base.metadata.create_all(engine)
    return engine


def test_gap_resolve_cli_recovers_failed_delivery_to_pending(tmp_path):
    db_path = tmp_path / "dark-launch-gap-resolve.sqlite3"
    engine = _sqlite_engine(db_path)
    factory = sessionmaker(bind=engine, class_=Session, autoflush=False, expire_on_commit=False, future=True)
    ids = _uuid_stream()

    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        envelope = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="start",
            source_request_identity="cli-gap-resolve",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={"ok": True, "command": "start", "code": "OK"},
            source_post_state={"selected_tables": [], "tables": {}},
            rollout_sequence=1,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        token = uuid.uuid4()
        claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=token,
            now=NOW,
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        gap = service.fail_delivery(
            delivery_id=claim.delivery_id,
            claim_token=token,
            claim_revision=claim.delivery_revision,
            worker_id="worker-1",
            error="AbandonmentAttempt has no attribute 'operation_id'",
            failed_at=NOW + timedelta(seconds=1),
        )
        gap_id = gap.gap_id
        failed_revision = gap.details["failed_delivery_revision"]

    exit_code = main(
        [
            "gap-resolve",
            "--database-url",
            f"sqlite+pysqlite:///{db_path}",
            "--gap-id",
            str(gap_id),
            "--reason",
            "shadow_worker.py:441 typo fixed; dark launch has no external effects, so not-applied",
        ]
    )
    assert exit_code == 0

    with session_scope(factory) as session:
        resolved_gap = session.get(tx.ShadowGap, gap_id)
        delivery = session.scalar(
            select(tx.ShadowDelivery).where(tx.ShadowDelivery.envelope_id == envelope.envelope_id)
        )
        assert resolved_gap.state == "resolved"
        assert resolved_gap.resolution["delivery_outcome"] == "not_applied"
        assert delivery.state == "pending"
        assert delivery.delivery_revision == failed_revision + 1

    engine.dispose()


def test_gap_resolve_cli_rejects_immutable_skipped_create(tmp_path):
    db_path = tmp_path / "dark-launch-create-gap-resolve.sqlite3"
    engine = _sqlite_engine(db_path)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    ids = _uuid_stream()

    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        envelope = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="create",
            source_request_identity="capture-only-create",
            canonical_input={"command": "create", "arguments": {"title": "Captured"}},
            source_outcome={"ok": True, "data": {"task_gid": "1217304161066314"}},
            source_post_state={"captured": True},
            rollout_sequence=1,
            source_authority_generation="legacy-1",
            pinned_inputs={"rollout_mode": "execute"},
            capture_qualification="capture_only",
            captured_at=NOW,
        )
        token = uuid.uuid4()
        claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=token,
            now=NOW,
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        service.skip_delivery(
            delivery_id=claim.delivery_id,
            claim_token=token,
            claim_revision=claim.delivery_revision,
            worker_id="worker-1",
            reason="dark-launch treatment is capture_only",
            comparator_release="before-create-correlation",
            completed_at=NOW + timedelta(seconds=1),
        )
        gap = session.scalar(
            select(tx.ShadowGap).where(tx.ShadowGap.envelope_id == envelope.envelope_id)
        )
        gap_id = gap.gap_id

    with pytest.raises(
        TransitionAuthorityError,
        match="superseding replay mechanism is required",
    ):
        main(
            [
                "gap-resolve",
                "--database-url",
                f"sqlite+pysqlite:///{db_path}",
                "--gap-id",
                str(gap_id),
                "--reason",
                "create correlation is now qualified",
            ]
        )

    with session_scope(factory) as session:
        resolved_gap = session.get(tx.ShadowGap, gap_id)
        current_envelope = session.get(tx.ShadowEnvelope, envelope.envelope_id)
        delivery = session.scalar(
            select(tx.ShadowDelivery).where(
                tx.ShadowDelivery.envelope_id == envelope.envelope_id
            )
        )
        comparison = session.scalar(
            select(tx.ShadowComparison).where(
                tx.ShadowComparison.envelope_id == envelope.envelope_id
            )
        )
        assert resolved_gap.state == "open"
        assert resolved_gap.resolution is None
        assert current_envelope.capture_qualification == "capture_only"
        assert delivery.state == "delivered"
        assert comparison is not None
        assert comparison.target_result == {
            "shadow_execution": "skipped",
            "reason": "dark-launch treatment is capture_only",
        }

    engine.dispose()
