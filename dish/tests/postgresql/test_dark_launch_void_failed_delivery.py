"""Terminal failed shadow delivery operator-void behavior and CLI wiring."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg.dark_launch import main
from dish_pg.database import session_scope
from dish_pg.transition import ShadowService, TransitionAuthorityError
from tests.support.postgresql.core import _bootstrap_registry, _next, _uuid_stream, core_db
from tests.support.postgresql.workflow import NOW


def _capture(
    service: ShadowService,
    *,
    baseline_id: uuid.UUID,
    source_identity: str,
    rollout_sequence: int,
):
    envelope = service.capture_envelope(
        shadow_baseline_id=baseline_id,
        command_name="start",
        source_request_identity=source_identity,
        canonical_input={"command": "start", "arguments": {}},
        source_outcome={"ok": True, "command": "start", "code": "OK"},
        source_post_state={"selected_tables": [], "tables": {}},
        rollout_sequence=rollout_sequence,
        source_authority_generation="legacy-1",
        captured_at=NOW + timedelta(seconds=rollout_sequence),
    )
    return service.session.scalar(
        select(tx.ShadowDelivery).where(tx.ShadowDelivery.envelope_id == envelope.envelope_id)
    )


def _fail(
    service: ShadowService,
    *,
    delivery: tx.ShadowDelivery,
    worker_id: str,
    token: uuid.UUID,
    now_offset: int,
):
    claimed = service.claim_delivery(
        worker_id=worker_id,
        claim_token=token,
        now=NOW + timedelta(seconds=now_offset),
        ttl=timedelta(minutes=2),
        shadow_baseline_id=None,
    )
    assert claimed is not None
    assert claimed.delivery_id == delivery.delivery_id
    service.fail_delivery(
        delivery_id=claimed.delivery_id,
        claim_token=token,
        claim_revision=claimed.delivery_revision,
        worker_id=worker_id,
        error="capture schema 2 is unevaluable under the current resolver",
        failed_at=NOW + timedelta(seconds=now_offset + 1),
    )
    return service.session.get(tx.ShadowDelivery, delivery.delivery_id)


def test_void_failed_delivery_unblocks_later_rollout_and_records_honest_gap(core_db):
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="archive-head",
            created_at=NOW,
        )
        earlier = _capture(
            service,
            baseline_id=baseline.shadow_baseline_id,
            source_identity="terminal-failed-earlier",
            rollout_sequence=1,
        )
        later = _capture(
            service,
            baseline_id=baseline.shadow_baseline_id,
            source_identity="eligible-later",
            rollout_sequence=2,
        )
        failed = _fail(
            service,
            delivery=earlier,
            worker_id="worker-1",
            token=_next(ids),
            now_offset=10,
        )
        failed_revision = failed.delivery_revision
        failed_error = failed.last_error

        blocked = service.claim_delivery(
            worker_id="worker-2",
            claim_token=_next(ids),
            now=NOW + timedelta(seconds=20),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert blocked is None

        comparison = service.void_failed_delivery(
            delivery_id=earlier.delivery_id,
            reason="Historical capture_schema=2 evidence is terminally unevaluable; abandon evaluation.",
            comparator_release="dish-operator-test",
            completed_at=NOW + timedelta(seconds=30),
        )
        voided = session.get(tx.ShadowDelivery, earlier.delivery_id)
        assert voided.state == "delivered"
        assert voided.delivery_revision == failed_revision + 1
        assert voided.last_error == failed_error
        assert comparison.parity_class == "gap"
        assert comparison.target_result == {
            "shadow_execution": "not_evaluated",
            "settlement": "operator_voided",
            "evaluation_abandoned": True,
        }
        assert comparison.differences[0]["audit_kind"] == "operator_voided"
        assert comparison.differences[0]["reason"].startswith("Historical capture_schema=2")
        assert comparison.differences[0]["evaluation_abandoned"] is True

        operator_gap = session.scalar(
            select(tx.ShadowGap).where(
                tx.ShadowGap.envelope_id == comparison.envelope_id,
                tx.ShadowGap.gap_identity == comparison.differences[0]["gap_identity"],
            )
        )
        assert operator_gap is not None
        assert operator_gap.gap_kind == "delivery_failure"
        assert operator_gap.gap_identity.startswith("operator_voided:")
        assert operator_gap.details["audit_kind"] == "operator_voided"
        assert operator_gap.details["reason"] == comparison.differences[0]["reason"]
        assert operator_gap.details["evaluation_abandoned"] is True
        assert operator_gap.gap_kind != "uncomparable"

        normal_skip_gap_count = int(
            session.scalar(
                select(func.count()).select_from(tx.ShadowGap).where(
                    tx.ShadowGap.envelope_id == comparison.envelope_id,
                    tx.ShadowGap.gap_kind == "uncomparable",
                )
            )
            or 0
        )
        assert normal_skip_gap_count == 0
        assert session.scalar(select(func.count()).select_from(tx.ProjectionEpoch)) == 0
        assert session.get(models.AuthorityGeneration, context["generation_id"]).status == "active"

        next_token = _next(ids)
        claimed_later = service.claim_delivery(
            worker_id="worker-2",
            claim_token=next_token,
            now=NOW + timedelta(seconds=31),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert claimed_later is not None
        assert claimed_later.delivery_id == later.delivery_id


@pytest.mark.parametrize("state", ["pending", "claimed", "delivered"])
def test_void_failed_delivery_rejects_every_supported_nonfailed_state(core_db, state):
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="archive-head",
            created_at=NOW,
        )
        delivery = _capture(
            service,
            baseline_id=baseline.shadow_baseline_id,
            source_identity=f"state-gate-{state}",
            rollout_sequence=1,
        )
        if state in {"claimed", "delivered"}:
            token = _next(ids)
            claimed = service.claim_delivery(
                worker_id="worker-state-gate",
                claim_token=token,
                now=NOW + timedelta(seconds=10),
                ttl=timedelta(minutes=2),
                shadow_baseline_id=baseline.shadow_baseline_id,
            )
            assert claimed is not None
            delivery = claimed
            if state == "delivered":
                service.skip_delivery(
                    delivery_id=claimed.delivery_id,
                    claim_token=token,
                    claim_revision=claimed.delivery_revision,
                    worker_id="worker-state-gate",
                    reason="ordinary capture-time skip",
                    comparator_release="dish-operator-test",
                    completed_at=NOW + timedelta(seconds=11),
                )
                delivery = session.get(tx.ShadowDelivery, claimed.delivery_id)

        with pytest.raises(
            TransitionAuthorityError,
            match=rf"requires terminal failed state; current state is {state}",
        ):
            service.void_failed_delivery(
                delivery_id=delivery.delivery_id,
                reason="must not bypass normal delivery state",
                comparator_release="dish-operator-test",
                completed_at=NOW + timedelta(seconds=20),
            )


def test_void_failed_delivery_requires_nonblank_operator_reason(core_db):
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="archive-head",
            created_at=NOW,
        )
        delivery = _capture(
            service,
            baseline_id=baseline.shadow_baseline_id,
            source_identity="blank-reason",
            rollout_sequence=1,
        )
        failed = _fail(
            service,
            delivery=delivery,
            worker_id="worker-blank-reason",
            token=_next(ids),
            now_offset=10,
        )
        with pytest.raises(TransitionAuthorityError, match="operator void reason is required"):
            service.void_failed_delivery(
                delivery_id=failed.delivery_id,
                reason="   ",
                comparator_release="dish-operator-test",
                completed_at=NOW + timedelta(seconds=20),
            )
        assert session.get(tx.ShadowDelivery, failed.delivery_id).state == "failed"


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


def test_void_failed_delivery_cli_requires_reason_and_warns_evaluation_is_permanent(capsys):
    with pytest.raises(SystemExit) as help_exit:
        main(["void-failed-delivery", "--help"])
    assert help_exit.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Permanently gives up on evaluating" in help_text
    assert "does not record a real comparison or transfer authority" in help_text

    with pytest.raises(SystemExit) as missing_reason:
        main(
            [
                "void-failed-delivery",
                "--database-url",
                "sqlite+pysqlite:///:memory:",
                "--delivery-id",
                str(uuid.uuid4()),
                "--comparator-release",
                "dish-operator-test",
            ]
        )
    assert missing_reason.value.code == 2
    assert "--reason" in capsys.readouterr().err


def test_void_failed_delivery_cli_outputs_distinct_operator_audit(tmp_path, capsys):
    db_path = tmp_path / "dark-launch-void-failed.sqlite3"
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
            source_commit="archive-head",
            created_at=NOW,
        )
        delivery = _capture(
            service,
            baseline_id=baseline.shadow_baseline_id,
            source_identity="cli-operator-void",
            rollout_sequence=1,
        )
        failed = _fail(
            service,
            delivery=delivery,
            worker_id="worker-cli",
            token=_next(ids),
            now_offset=10,
        )
        delivery_id = failed.delivery_id

    exit_code = main(
        [
            "void-failed-delivery",
            "--database-url",
            f"sqlite+pysqlite:///{db_path}",
            "--delivery-id",
            str(delivery_id),
            "--reason",
            "Operator accepts permanent loss of evaluation for this historical envelope.",
            "--comparator-release",
            "dish-operator-test",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "void_failed_delivery"
    assert payload["delivery_state"] == "delivered"
    assert payload["parity_class"] == "gap"
    assert payload["evaluation_abandoned"] is True
    assert payload["gap_kind"] == "delivery_failure"
    assert payload["gap_audit_kind"] == "operator_voided"
    assert payload["reason"].startswith("Operator accepts permanent loss")

    with session_scope(factory) as session:
        comparison = session.scalar(select(tx.ShadowComparison))
        assert comparison.target_result["shadow_execution"] == "not_evaluated"
        assert comparison.differences[0]["audit_kind"] == "operator_voided"
        assert comparison.differences[0]["reason"] == payload["reason"]
        operator_gap = session.scalar(
            select(tx.ShadowGap).where(tx.ShadowGap.gap_identity.like("operator_voided:%"))
        )
        assert operator_gap is not None
        assert operator_gap.details["reason"] == payload["reason"]
        assert operator_gap.details["audit_kind"] == "operator_voided"

    engine.dispose()
