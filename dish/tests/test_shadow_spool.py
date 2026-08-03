from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dish_service.shadow_spool import ShadowSpool, ShadowSpoolConflict

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)


def _reserve(spool: ShadowSpool, identity: str = "request-1"):
    return spool.reserve(
        source_request_identity=identity,
        source_authority_generation="legacy-prod@generation-1",
        command_name="prepare",
        treatment="execute",
        canonical_input={"command": "prepare", "arguments": {"task_gid": "123"}},
        principal={"owner_id": "owner-1", "run_id": "run-1", "scope": "agent"},
        source_pre_state={"task_gid": "123", "operation": {"phase": "prepare"}},
        pinned_inputs={"now": NOW.isoformat()},
        created_at=NOW,
    )


def test_spool_reserves_monotonic_sequence_and_exact_replay(tmp_path) -> None:
    spool = ShadowSpool(tmp_path / "shadow.sqlite3")
    first = _reserve(spool)
    replay = _reserve(spool)
    second = _reserve(spool, "request-2")

    assert replay == first
    assert first.rollout_sequence == 1
    assert second.rollout_sequence == 2
    with pytest.raises(ShadowSpoolConflict):
        spool.reserve(
            source_request_identity="request-1",
            source_authority_generation="legacy-prod@generation-1",
            command_name="submit",
            treatment="execute",
            canonical_input={"command": "submit"},
            principal={"owner_id": "owner-1", "run_id": "run-1", "scope": "agent"},
            source_pre_state={},
            pinned_inputs={"now": NOW.isoformat()},
            created_at=NOW,
        )


def test_spool_completion_delivery_and_failure_retry_are_durable(tmp_path) -> None:
    spool = ShadowSpool(tmp_path / "shadow.sqlite3")
    reservation = _reserve(spool)
    spool.complete(
        reservation.registration_id,
        source_outcome={"ok": True, "code": "OK"},
        source_post_state={"operation": {"phase": "await_verification"}},
        source_effects={"write_attempt": "confirmed"},
        completed_at=NOW + timedelta(seconds=1),
    )
    item = spool.pending()[0]
    assert item.rollout_sequence == 1
    assert item.source_outcome == {"code": "OK", "ok": True}

    spool.mark_delivery_failed(item.registration_id, error="postgres unavailable")
    assert spool.pending()[0].delivery_attempts == 1
    spool.mark_delivered(item.registration_id, delivered_at=NOW + timedelta(seconds=2))
    assert spool.pending() == ()
    assert spool.status()["counts"]["delivered"] == 1


def test_stale_reservation_becomes_permanent_gap(tmp_path) -> None:
    spool = ShadowSpool(tmp_path / "shadow.sqlite3")
    reservation = _reserve(spool)
    assert spool.recover_stale_reservations(
        now=NOW + timedelta(minutes=10), older_than=timedelta(minutes=5)
    ) == 1
    item = spool.pending()[0]
    assert item.registration_id == reservation.registration_id
    assert item.state == "gap"
    assert item.gap["failure_stage"] == "command_completion_capture"
