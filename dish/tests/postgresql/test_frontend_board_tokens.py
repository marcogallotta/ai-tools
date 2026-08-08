from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from dish_service.frontend_tokens import (
    CursorInvalid,
    CursorStale,
    MAX_CURSOR_LENGTH,
    open_cursor,
    route_identity,
    seal_cursor,
)

SECRET = b"stage-3-test-token-secret-32-bytes-minimum"
NOW = datetime(2026, 8, 7, 20, 0, tzinfo=timezone.utc)


def test_route_identity_is_stable_typed_environment_scoped_and_non_raw() -> None:
    task_id = UUID("12345678-1234-5678-1234-567812345678")
    first = route_identity(secret=SECRET, environment="test", kind="task", object_id=task_id)
    second = route_identity(secret=SECRET, environment="test", kind="task", object_id=task_id)
    section = route_identity(secret=SECRET, environment="test", kind="section", object_id=task_id)
    production = route_identity(secret=SECRET, environment="production", kind="task", object_id=task_id)

    assert first == second
    assert first != section
    assert first != production
    assert str(task_id) not in first
    assert task_id.hex not in first


def test_cursor_is_opaque_tamper_resistant_and_environment_bound() -> None:
    payload = {
        "type": "board-section-continuation",
        "section_internal_id": "12345678-1234-5678-1234-567812345678",
        "after_sort_title": "secret internal sort boundary",
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
    }
    token = seal_cursor(secret=SECRET, environment="test", payload=payload)

    assert payload["section_internal_id"] not in token
    assert payload["after_sort_title"] not in token
    assert open_cursor(token, secret=SECRET, environment="test", now=NOW) == payload
    with pytest.raises(CursorInvalid):
        open_cursor(token, secret=SECRET, environment="production", now=NOW)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(CursorInvalid):
        open_cursor(tampered, secret=SECRET, environment="test", now=NOW)


def test_cursor_expiry_and_maximum_title_payload_remain_bounded() -> None:
    expired = seal_cursor(
        secret=SECRET,
        environment="test",
        payload={"expires_at": (NOW - timedelta(seconds=1)).isoformat()},
    )
    with pytest.raises(CursorStale):
        open_cursor(expired, secret=SECRET, environment="test", now=NOW)

    maximum_title = "😀" * 500
    token = seal_cursor(
        secret=SECRET,
        environment="test",
        payload={
            "after_sort_title": maximum_title,
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    assert len(token) <= MAX_CURSOR_LENGTH
    assert open_cursor(token, secret=SECRET, environment="test", now=NOW)[
        "after_sort_title"
    ] == maximum_title
