from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from dish_pg import test_journey_qualifier as journey


RUN_ID = "11111111-1111-4111-8111-111111111111"
DISH_ID = "22222222-2222-4222-8222-222222222222"
GENERATION_ID = "33333333-3333-4333-8333-333333333333"


def _target() -> journey.TestTarget:
    return journey.TestTarget(
        action_url="http://test/action",
        health_url="http://test/health",
        token="top-secret-token",
        database="dish_native_test",
        generation_id=GENERATION_ID,
        schema_head="0042_scalar_dish_state",
        dish_release="dish-release-test",
    )


def _health(*_args: object) -> dict[str, object]:
    return {
        "ok": True,
        "startup_ready": True,
        "backend": "postgresql",
        "profile": "test",
        "identity": {
            "database": "dish_native_test",
            "generation_id": GENERATION_ID,
            "generation_status": "active",
            "schema_head": "0042_scalar_dish_state",
            "dish_release": "dish-release-test",
        },
        "isolation": {"asana_environment_keys": []},
    }


class FakeClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, command: str, **arguments: object) -> dict[str, object]:
        self.calls.append((command, arguments))
        if command == "create":
            return {"ok": True, "command": command, "data": {"dish_id": DISH_ID}}
        if command == "search":
            return {
                "ok": True,
                "command": command,
                "data": {"results": [{"dish_id": DISH_ID, "title": arguments["query"]}]},
            }
        if command == "read":
            return {
                "ok": True,
                "command": command,
                "data": {"dish_id": DISH_ID, "title": "Dish TEST journey 20260902T120000Z abcdef0123"},
            }
        if len([item for item in self.calls if item[0] == "start"]) == 1:
            return {
                "ok": False,
                "command": command,
                "code": "CONFIRMATION_REQUIRED",
                "data": {"intent_challenge_id": "44444444-4444-4444-8444-444444444444"},
            }
        return {
            "ok": True,
            "command": command,
            "data": {"operation_id": "55555555-5555-4555-8555-555555555555"},
        }


def test_live_journey_uses_canonical_identity_and_emits_redacted_pass_receipt() -> None:
    client = FakeClient()
    receipt = journey.run_journey(
        _target(),
        run_id=RUN_ID,
        health_reader=_health,
        client_factory=lambda *_args, **_kwargs: client,
        now=lambda: datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        uuid_factory=lambda: uuid.UUID("abcdef01-2345-4678-8123-456789abcdef"),
    )

    assert receipt["status"] == "PASS"
    assert receipt["fixture"] == {
        "dish_id": DISH_ID,
        "retained": True,
        "title": "Dish TEST journey 20260902T120000Z abcdef0123",
    }
    assert [command for command, _ in client.calls] == [
        "create", "search", "read", "start", "start"
    ]
    assert client.calls[2][1]["dish_id"] == DISH_ID
    assert client.calls[3][1]["dish_id"] == DISH_ID
    assert "top-secret-token" not in str(receipt)
    assert RUN_ID not in str(receipt)


def test_preflight_rejects_non_test_health_before_constructing_client() -> None:
    health = _health()
    health["profile"] = "prod"
    with pytest.raises(journey.JourneyError, match="isolated PostgreSQL TEST"):
        journey.run_journey(
            _target(),
            run_id=RUN_ID,
            health_reader=lambda *_args: health,
            client_factory=lambda *_args, **_kwargs: pytest.fail("client constructed"),
        )


def test_preflight_rejects_health_release_identity_drift() -> None:
    health = _health()
    health["identity"]["dish_release"] = "different-release"
    with pytest.raises(journey.JourneyError, match="configured active TEST generation"):
        journey.run_journey(_target(), run_id=RUN_ID, health_reader=lambda *_args: health)


def test_failure_after_create_names_retained_fixture_without_secrets() -> None:
    class MissingSearch(FakeClient):
        def execute(self, command: str, **arguments: object) -> dict[str, object]:
            result = super().execute(command, **arguments)
            if command == "search":
                result["data"] = {"results": []}
            return result

    receipt = journey.run_journey(
        _target(), run_id=RUN_ID, health_reader=_health, client_factory=MissingSearch
    )
    assert receipt["status"] == "FAIL"
    assert receipt["stage"] == "search"
    assert receipt["fixture"]["dish_id"] == DISH_ID
    assert receipt["fixture"]["retained"] is True
    assert "top-secret-token" not in str(receipt)


def test_run_id_must_be_operator_supplied_canonical_uuid() -> None:
    with pytest.raises(journey.JourneyError, match="operator-supplied canonical UUID"):
        journey.run_journey(_target(), run_id="", health_reader=_health)
