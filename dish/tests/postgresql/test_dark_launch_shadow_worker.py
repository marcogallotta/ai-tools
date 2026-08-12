from __future__ import annotations
import uuid
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.planner import AuthoritativeSnapshot, CanonicalCommandIntent, plan_command
from dish_pg.shadow_worker import (
    CommandPortShadowEvaluator,
    ShadowIdentityMappingError,
    ShadowWorker,
    _translate_workflow_identifiers,
)
from dish_pg.transition import ShadowService
from dish_pg.workflow import sha256_json
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.command import (
    _add_verification_queue,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db

from tests.support.postgresql.dark_launch_shadow_worker import (
    Evaluator,
    _spool,
    _real_verification_target,
)


def test_supply_evidence_shadow_replay_preserves_captured_principal_scope(monkeypatch):
    import dish_pg.shadow_worker as shadow_worker

    calls = []
    generation_id = uuid.uuid4()

    class Reads:
        def active_generation(self):
            return SimpleNamespace(generation_id=generation_id)

    class Port:
        def __init__(self, *_args, **_kwargs):
            self.reads = Reads()

        def execute(self, call):
            calls.append(call)
            return SimpleNamespace(
                ok=True,
                command=call.command_name,
                code="OK",
                http_status=200,
                data={},
                retryable=False,
            )

    class Session:
        def get(self, model, _identity):
            assert model is tx.ShadowBaseline
            return SimpleNamespace(status="open", generation_id=generation_id)

        def flush(self):
            return None

    monkeypatch.setattr(shadow_worker, "PostgresCommandPort", Port)
    monkeypatch.setattr(
        shadow_worker,
        "_translate_workflow_identifiers",
        lambda _session, _envelope, arguments: dict(arguments),
    )
    monkeypatch.setattr(shadow_worker, "_ensure_shadow_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        shadow_worker, "_target_authority_state", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        shadow_worker, "_target_response_payload", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(shadow_worker, "canonical_transition", lambda *_args: {})

    evaluator = CommandPortShadowEvaluator(cursor_secret=b"test")
    for principal_class in ("admin", "agent"):
        evaluator.evaluate(
            Session(),
            SimpleNamespace(
                canonical_input={"command": "supply-evidence", "arguments": {}},
                shadow_baseline_id=uuid.uuid4(),
                principal={
                    "owner_id": "owner",
                    "run_id": "source-run",
                    "principal_class": principal_class,
                },
                source_request_identity=f"{principal_class}-supply-evidence",
                source_authority_generation="legacy-1",
                command_name="supply-evidence",
                captured_at=NOW,
            ),
        )

    assert [call.command_name for call in calls] == ["supply-evidence", "supply-evidence"]
    assert [call.principal_class for call in calls] == ["admin", "agent"]

    snapshot = AuthoritativeSnapshot(
        generation_id=str(generation_id),
        task_id=None,
        fence=None,
        workflow=None,
        task_exists=False,
    )
    admin_plan = plan_command(
        snapshot=snapshot,
        intent=CanonicalCommandIntent(
            command_name="supply-evidence",
            arguments={},
            principal_class=calls[0].principal_class,
            owner_id=calls[0].owner_id,
            run_id=str(calls[0].run_id),
        ),
        pinned_now=NOW,
    )
    agent_plan = plan_command(
        snapshot=snapshot,
        intent=CanonicalCommandIntent(
            command_name="supply-evidence",
            arguments={},
            principal_class=calls[1].principal_class,
            owner_id=calls[1].owner_id,
            run_id=str(calls[1].run_id),
        ),
        pinned_now=NOW,
    )
    assert admin_plan.result_code == "TASK_NOT_FOUND"
    assert agent_plan.result_code == "PRINCIPAL_SCOPE_MISMATCH"


def test_shadow_worker_delivers_executes_and_compares(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline=ShadowService(session, uuid_factory=lambda:_next(ids)).create_baseline(
            generation_id=context["generation_id"], source_generation_identity="legacy-1",
            source_commit="worktree", created_at=NOW)
        baseline_id=baseline.shadow_baseline_id
    spool=_spool(tmp_path)
    worker=ShadowWorker(spool=spool, session_maker=factory, baseline_id=baseline_id,
                        evaluator=Evaluator(), worker_id="shadow-1", comparator_release="test",
                        kill_switch_path=tmp_path/"dark-launch.disabled", clock=lambda:NOW)
    assert worker.run_once() is True
    with session_scope(factory) as session:
        comparison=session.scalar(select(tx.ShadowComparison))
        assert comparison.parity_class == "exact"
    assert spool.status()["counts"]["delivered"] == 1

def test_capture_only_is_settled_as_explicit_uncomparable_gap(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline=ShadowService(session, uuid_factory=lambda:_next(ids)).create_baseline(
            generation_id=context["generation_id"], source_generation_identity="legacy-1",
            source_commit="worktree", created_at=NOW)
        baseline_id=baseline.shadow_baseline_id
    worker=ShadowWorker(spool=_spool(tmp_path,treatment="capture_only",command_name="recover"), session_maker=factory,
                        baseline_id=baseline_id, evaluator=Evaluator(), worker_id="shadow-1",
                        comparator_release="test", kill_switch_path=tmp_path/"dark-launch.disabled",
                        clock=lambda:NOW)
    worker.run_once()
    with session_scope(factory) as session:
        assert session.scalar(select(tx.ShadowComparison)).parity_class == "gap"
        assert session.scalar(select(tx.ShadowGap)).gap_kind == "uncomparable"


def test_captured_treatment_contradiction_is_reported_not_silently_compared(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"], source_generation_identity="legacy-1",
            source_commit="worktree", created_at=NOW)
        baseline_id = baseline.shadow_baseline_id
    worker = ShadowWorker(
        spool=_spool(tmp_path, treatment="capture_only", command_name="prepare"),
        session_maker=factory, baseline_id=baseline_id, evaluator=Evaluator(),
        worker_id="shadow-1", comparator_release="test",
        kill_switch_path=tmp_path/"dark-launch.disabled", clock=lambda: NOW,
    )

    assert worker.run_once() is True
    with session_scope(factory) as session:
        delivery = session.scalar(select(tx.ShadowDelivery))
        assert delivery.state == "failed"
        assert "contradicts authoritative command treatment" in delivery.last_error
        assert session.scalar(select(tx.ShadowComparison)) is None

def test_kill_switch_stops_worker_before_spool_delivery(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id

    spool = _spool(tmp_path)
    kill_switch = tmp_path / "dark-launch.disabled"
    kill_switch.write_text("disabled\n")
    worker = ShadowWorker(
        spool=spool,
        session_maker=factory,
        baseline_id=baseline_id,
        evaluator=Evaluator(),
        worker_id="shadow-1",
        comparator_release="test",
        kill_switch_path=kill_switch,
        clock=lambda: NOW,
    )

    assert worker.run_once() is False
    assert spool.status()["counts"]["complete"] == 1
    with session_scope(factory) as session:
        assert session.scalar(select(tx.ShadowEnvelope)) is None

def test_shadow_worker_compacts_delivered_payload_after_retention(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id
    spool = _spool(tmp_path)
    worker = ShadowWorker(
        spool=spool,
        session_maker=factory,
        baseline_id=baseline_id,
        evaluator=Evaluator(),
        worker_id="shadow-1",
        comparator_release="test",
        kill_switch_path=tmp_path / "dark-launch.disabled",
        delivered_retention=timedelta(0),
        clock=lambda: NOW,
    )
    assert worker.run_once() is True
    assert spool.status()["counts"]["delivered"] == 0
    assert spool.status()["counts"]["archived"] == 1

def test_shadow_worker_recovers_stale_reservation_before_later_delivery(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id
    spool = ShadowSpool(tmp_path / "spool.sqlite3", min_free_bytes=1)
    first = spool.reserve(
        source_request_identity="request-first",
        source_authority_generation="legacy-1",
        command_name="prepare",
        treatment="execute",
        canonical_input={"command": "prepare", "arguments": {}},
        principal={},
        source_pre_state={},
        pinned_inputs={"rollout_mode": "execute"},
        created_at=NOW,
    )
    second = spool.reserve(
        source_request_identity="request-second",
        source_authority_generation="legacy-1",
        command_name="prepare",
        treatment="execute",
        canonical_input={"command": "prepare", "arguments": {}},
        principal={},
        source_pre_state={},
        pinned_inputs={"rollout_mode": "execute"},
        created_at=NOW,
    )
    spool.complete(
        second.registration_id,
        source_outcome={"ok": True},
        source_post_state={"phase": "verification"},
        source_effects={},
        completed_at=NOW,
    )
    worker = ShadowWorker(
        spool=spool,
        session_maker=factory,
        baseline_id=baseline_id,
        evaluator=Evaluator(),
        worker_id="shadow-1",
        comparator_release="test",
        kill_switch_path=tmp_path / "dark-launch.disabled",
        reservation_ttl=timedelta(seconds=90),
        clock=lambda: NOW + timedelta(seconds=91),
    )
    assert worker.run_once() is True
    status = spool.status()["counts"]
    assert status["delivered"] == 1
    assert status["complete"] == 1
    with session_scope(factory) as session:
        gap = session.scalar(select(tx.ShadowGap))
        assert gap is not None
        assert gap.gap_identity == "capture:request-first"
    assert first.rollout_sequence < second.rollout_sequence
