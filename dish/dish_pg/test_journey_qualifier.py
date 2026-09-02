"""Small live qualifier for the native PostgreSQL TEST Action journey."""
from __future__ import annotations

import json
import shlex
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from dish_service.client import DishActionClient
from dish_tool.errors import DishRuleError


DEFAULT_ENV_PATH = Path("/home/marco/.config/dish-service/test.env")
DEFAULT_ACTION_URL = "http://127.0.0.1:8766/test"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8765/health"
RECEIPT_FORMAT = "dish-native-test-journey-v1"


class JourneyError(RuntimeError):
    """The live journey could not prove its narrow qualification claim."""


@dataclass(frozen=True)
class TestTarget:
    action_url: str
    health_url: str
    token: str
    database: str
    generation_id: str
    schema_head: str
    dish_release: str


def _read_env(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise JourneyError(f"cannot read TEST environment {path}: {exc}") from exc
    values: dict[str, str] = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        if not separator or not key.strip():
            raise JourneyError(f"{path}:{number}: invalid environment assignment")
        try:
            parsed = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise JourneyError(f"{path}:{number}: invalid environment value") from exc
        if len(parsed) > 1:
            raise JourneyError(f"{path}:{number}: environment value must be one token")
        values[key.strip()] = parsed[0] if parsed else ""
    return values


def load_test_target(
    path: Path = DEFAULT_ENV_PATH,
    *,
    action_url: str = DEFAULT_ACTION_URL,
    health_url: str = DEFAULT_HEALTH_URL,
) -> TestTarget:
    values = _read_env(path)

    def required(name: str) -> str:
        value = values.get(name, "").strip()
        if not value:
            raise JourneyError(f"TEST environment is missing {name}")
        return value

    if required("DISH_PROFILE") != "test":
        raise JourneyError("qualifier requires DISH_PROFILE=test")
    if required("DISH_AUTHORITY_BACKEND") != "postgresql":
        raise JourneyError("qualifier requires PostgreSQL authority")
    database = required("DISH_PG_EXPECTED_DATABASE_NAME")
    if not database.endswith("_test"):
        raise JourneyError("qualifier database identity must end in _test")
    generation_id = required("DISH_PG_EXPECTED_GENERATION_ID")
    try:
        if str(uuid.UUID(generation_id)) != generation_id:
            raise ValueError
    except ValueError as exc:
        raise JourneyError("TEST generation identity must be a canonical UUID") from exc
    return TestTarget(
        action_url=action_url.rstrip("/"),
        health_url=health_url,
        token=required("DISH_SERVICE_ACTION_TOKEN"),
        database=database,
        generation_id=generation_id,
        schema_head=required("DISH_PG_EXPECTED_SCHEMA_HEAD"),
        dish_release=required("DISH_PG_EXPECTED_RELEASE"),
    )


def read_health(url: str, *, timeout: float = 10.0) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise JourneyError(f"TEST health request failed: {type(exc).__name__}") from exc
    if not isinstance(payload, Mapping):
        raise JourneyError("TEST health response is not an object")
    return payload


def _preflight(target: TestTarget, health: Mapping[str, Any]) -> Mapping[str, str]:
    identity = health.get("identity")
    isolation = health.get("isolation")
    if (
        health.get("ok") is not True
        or health.get("startup_ready") is not True
        or health.get("backend") != "postgresql"
        or health.get("profile") != "test"
        or not isinstance(identity, Mapping)
        or not isinstance(isolation, Mapping)
        or isolation.get("asana_environment_keys") not in ([], ())
    ):
        raise JourneyError("health does not prove isolated PostgreSQL TEST authority")
    observed = {
        "database": str(identity.get("database", "")),
        "generation_id": str(identity.get("generation_id", "")),
        "generation_status": str(identity.get("generation_status", "")),
        "schema_head": str(identity.get("schema_head", "")),
        "dish_release": str(identity.get("dish_release", "")),
    }
    expected = {
        "database": target.database,
        "generation_id": target.generation_id,
        "generation_status": "active",
        "schema_head": target.schema_head,
        "dish_release": target.dish_release,
    }
    if observed != expected:
        raise JourneyError("health identity does not match the configured active TEST generation")
    return observed


def _require_ok(result: Mapping[str, Any], command: str) -> Mapping[str, Any]:
    if result.get("ok") is not True or result.get("command") != command:
        data = result.get("data")
        message = data.get("message") if isinstance(data, Mapping) else None
        detail = f": {message}" if isinstance(message, str) and message else ""
        raise JourneyError(f"{command} failed with {result.get('code', 'UNKNOWN')}{detail}")
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise JourneyError(f"{command} returned no data object")
    return data


def run_journey(
    target: TestTarget,
    *,
    run_id: str,
    health_reader: Callable[[str], Mapping[str, Any]] = read_health,
    client_factory: Callable[..., Any] = DishActionClient,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> dict[str, Any]:
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError
    except ValueError as exc:
        raise JourneyError("--run-id must be an operator-supplied canonical UUID") from exc

    authority = _preflight(target, health_reader(target.health_url))
    client = client_factory(target.action_url, token=target.token, run_id=run_id)
    title = f"Dish TEST journey {now().strftime('%Y%m%dT%H%M%SZ')} {uuid_factory().hex[:10]}"
    fixture: dict[str, Any] = {"title": title, "retained": True}
    stage = "create"
    try:
        created = _require_ok(client.execute("create", agent="codex", title=title), "create")
        dish_id = str(created.get("dish_id", ""))
        if not dish_id or str(uuid.UUID(dish_id)) != dish_id:
            raise JourneyError("create did not return a canonical dish_id")
        fixture["dish_id"] = dish_id

        stage = "search"
        searched = _require_ok(
            client.execute("search", query=title, agent="codex", page_size=10),
            "search",
        )
        matches = searched.get("results")
        if not isinstance(matches, list) or not any(
            isinstance(item, Mapping)
            and item.get("dish_id") == dish_id
            and item.get("title") == title
            for item in matches
        ):
            raise JourneyError("Search did not return the created canonical Dish")

        stage = "read"
        read = _require_ok(client.execute("read", dish_id=dish_id, agent="codex"), "read")
        if read.get("dish_id") != dish_id or read.get("title") != title:
            raise JourneyError("Read did not return the created canonical Dish")

        stage = "start"
        first = client.execute("start", dish_id=dish_id, agent="codex", kind="planning")
        if first.get("code") == "CONFIRMATION_REQUIRED":
            first_data = first.get("data")
            challenge = first_data.get("intent_challenge_id") if isinstance(first_data, Mapping) else None
            if not isinstance(challenge, str):
                raise JourneyError("Start confirmation response omitted its continuation identity")
            started = _require_ok(
                client.execute(
                    "start",
                    dish_id=dish_id,
                    agent="codex",
                    kind="planning",
                    intent_challenge_id=challenge,
                    intent_basis="user_requested",
                ),
                "start",
            )
        else:
            started = _require_ok(first, "start")
        operation_id = str(started.get("operation_id", ""))
        if not operation_id:
            raise JourneyError("Start did not return an operation identity")
    except (DishRuleError, JourneyError, ValueError) as exc:
        return {
            "format": RECEIPT_FORMAT,
            "status": "FAIL",
            "environment": "TEST",
            "stage": stage,
            "error": str(exc),
            "fixture": fixture,
        }

    return {
        "format": RECEIPT_FORMAT,
        "status": "PASS",
        "environment": "TEST",
        "authority": authority,
        "journey": ["health", "create", "search", "read", "start"],
        "fixture": fixture,
        "start": {"operation_id": operation_id},
    }
