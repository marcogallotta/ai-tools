from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dish_pg.command_port import CommandRuleError, PostgresCommandPort

NOW = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)


class RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


def _port() -> PostgresCommandPort:
    port = object.__new__(PostgresCommandPort)
    port.session = RecordingSession()
    ids = iter(uuid.UUID(int=value) for value in range(100, 200))
    port.uuid_factory = lambda: next(ids)
    return port


def _lease() -> SimpleNamespace:
    return SimpleNamespace(
        lease_id=uuid.UUID(int=1),
        state="active",
        lease_revision=4,
        expires_at=NOW + timedelta(minutes=5),
        terminal_at=None,
    )


def _execution() -> SimpleNamespace:
    return SimpleNamespace(request_id=uuid.UUID(int=2), execution_id=uuid.UUID(int=3))


@pytest.mark.parametrize("terminal_kind", ["released", "expired", "recovered"])
def test_lease_terminal_event_matches_durable_terminal_state(terminal_kind: str) -> None:
    port = _port()
    lease = _lease()
    execution = _execution()

    port._terminalize_lease(lease, terminal_kind, execution, NOW, terminal_kind)

    assert lease.state == terminal_kind
    assert lease.lease_revision == 5
    assert lease.terminal_at == NOW
    assert len(port.session.added) == 1
    event = port.session.added[0]
    assert event.event_kind == terminal_kind
    assert event.prior_revision == 4
    assert event.resulting_revision == 5
    assert event.prior_expiry == lease.expires_at
    assert event.resulting_expiry == lease.expires_at


def test_exact_terminal_replay_does_not_append_duplicate_event() -> None:
    port = _port()
    lease = _lease()
    execution = _execution()

    port._terminalize_lease(lease, "expired", execution, NOW, "expire-lease")
    port._terminalize_lease(lease, "expired", execution, NOW, "expire-lease")

    assert lease.state == "expired"
    assert lease.lease_revision == 5
    assert [event.event_kind for event in port.session.added] == ["expired"]


def test_contradictory_terminal_replay_fails_without_new_event() -> None:
    port = _port()
    lease = _lease()
    execution = _execution()
    port._terminalize_lease(lease, "recovered", execution, NOW, "recover-lease")

    with pytest.raises(CommandRuleError) as conflict:
        port._terminalize_lease(lease, "released", execution, NOW, "release-lease")

    assert conflict.value.code == "LEASE_ALREADY_TERMINAL"
    assert lease.state == "recovered"
    assert lease.lease_revision == 5
    assert [event.event_kind for event in port.session.added] == ["recovered"]


def test_unsupported_terminal_kind_leaves_lease_constraints_intact() -> None:
    port = _port()
    lease = _lease()

    with pytest.raises(ValueError, match="unsupported lease terminal state"):
        port._terminalize_lease(lease, "released-ish", _execution(), NOW, "bad")

    assert lease.state == "active"
    assert lease.lease_revision == 4
    assert lease.terminal_at is None
    assert port.session.added == []
