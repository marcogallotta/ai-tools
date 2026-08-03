from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dish_service.shadow_spool import (
    ShadowSpool,
    ShadowSpoolCapacityError,
    ShadowSpoolConflict,
)

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


def test_spool_record_limit_rejects_new_identity_but_allows_exact_replay(tmp_path) -> None:
    spool = ShadowSpool(
        tmp_path / "shadow.sqlite3",
        max_records=1,
        min_free_bytes=1,
    )
    first = _reserve(spool)
    assert _reserve(spool) == first
    with pytest.raises(ShadowSpoolCapacityError, match="record limit"):
        _reserve(spool, "request-2")
    assert spool.status()["capacity"]["accepting_new_records"] is False


def test_compaction_removes_payload_but_preserves_replay_identity(tmp_path) -> None:
    spool = ShadowSpool(tmp_path / "shadow.sqlite3", min_free_bytes=1)
    reservation = _reserve(spool)
    spool.complete(
        reservation.registration_id,
        source_outcome={"ok": True, "large": "x" * 10_000},
        source_post_state={"phase": "done"},
        source_effects={},
        completed_at=NOW,
    )
    spool.mark_delivered(reservation.registration_id, delivered_at=NOW)
    assert spool.compact_delivered(
        now=NOW + timedelta(days=8), older_than=timedelta(days=7)
    ) == 1
    assert spool.get_by_source_identity("request-1") is None
    assert spool.has_source_identity("request-1") is True
    replay = _reserve(spool)
    assert replay.registration_id == reservation.registration_id
    assert replay.state == "delivered"
    with pytest.raises(ShadowSpoolConflict, match="compacted evidence"):
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
    assert spool.status()["counts"]["archived"] == 1


def test_pending_stops_at_earliest_reserved_sequence_until_recovered(tmp_path) -> None:
    spool = ShadowSpool(tmp_path / "shadow.sqlite3", min_free_bytes=1)
    first = _reserve(spool, "request-1")
    second = _reserve(spool, "request-2")
    spool.complete(
        second.registration_id,
        source_outcome={"ok": True},
        source_post_state={"phase": "later"},
        source_effects={},
        completed_at=NOW + timedelta(seconds=1),
    )
    assert spool.pending() == ()
    assert spool.recover_stale_reservations(
        now=NOW + timedelta(seconds=91), older_than=timedelta(seconds=90)
    ) == 1
    pending = spool.pending()
    assert [item.registration_id for item in pending] == [
        first.registration_id,
        second.registration_id,
    ]
    assert [item.state for item in pending] == ["gap", "complete"]


def test_open_existing_refuses_missing_path_without_creating_database(tmp_path) -> None:
    from dish_service.shadow_spool import ShadowSpoolError

    path = tmp_path / "mistyped.sqlite3"
    spool = ShadowSpool.open_existing(path, min_free_bytes=1)
    with pytest.raises(ShadowSpoolError, match="does not exist"):
        spool.status()
    assert not path.exists()


def test_completion_capacity_guard_rolls_back_large_payload(tmp_path) -> None:
    spool = ShadowSpool(
        tmp_path / "shadow.sqlite3",
        max_bytes=256 * 1024,
        min_free_bytes=1,
    )
    reservation = _reserve(spool)
    before = spool.status()["capacity"]["logical_bytes"]

    with pytest.raises(ShadowSpoolCapacityError, match="byte limit"):
        spool.complete(
            reservation.registration_id,
            source_outcome={"ok": True, "payload": "x" * (512 * 1024)},
            source_post_state={"phase": "verification"},
            source_effects={},
            completed_at=NOW,
        )

    item = spool.get_by_source_identity("request-1")
    assert item is not None
    assert item.state == "reserved"
    assert item.source_outcome is None
    assert spool.status()["capacity"]["logical_bytes"] < before + 128 * 1024
