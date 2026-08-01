"""Independent GPT Action contract expectations used by tests.

These literals intentionally do not import command or identifier constants from
production. They are the test-side statement of the public contract. Tests may
compare generated/runtime artifacts with these values, but must not derive the
expected inventory from the same implementation that produces the artifact.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

EXPECTED_ACTION_COMMANDS = (
    "create",
    "sections",
    "section-tasks",
    "read",
    "inspect",
    "start",
    "prepare",
    "approve",
    "reject",
    "submit",
    "renew-lease",
)
EXPECTED_REPLAY_SAFE_COMMANDS = frozenset(
    {"create", "start", "prepare", "approve", "reject", "submit", "renew-lease"}
)
EXPECTED_READ_ONLY_COMMANDS = frozenset({"sections", "section-tasks", "read", "inspect"})
EXPECTED_CONSEQUENTIAL = {
    command: command in EXPECTED_REPLAY_SAFE_COMMANDS
    for command in EXPECTED_ACTION_COMMANDS
}
EXPECTED_DISH_UUID_SCHEMA = {
    "type": "string",
    "format": "uuid",
    "pattern": (
        "^(?!00000000-0000-0000-0000-000000000000$)"
        "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    ),
    "minLength": 36,
    "maxLength": 36,
}


def request_schema(document: Mapping[str, Any], command: str) -> Mapping[str, Any]:
    return document["paths"][f"/v1/action/{command}"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]


def named_run_and_request_id_schemas(
    document: Mapping[str, Any],
) -> dict[tuple[str, ...], Mapping[str, Any]]:
    found: dict[tuple[str, ...], Mapping[str, Any]] = {}

    def collect(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = (*path, str(key))
                if key in {"run_id", "request_id"} and isinstance(child, Mapping):
                    found[child_path] = child
                collect(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                collect(child, (*path, str(index)))

    collect(document)
    return found


def expected_run_and_request_id_paths() -> set[tuple[str, ...]]:
    run_paths = {
        (
            "paths",
            f"/v1/action/{command}",
            "post",
            "requestBody",
            "content",
            "application/json",
            "schema",
            "properties",
            "client",
            "properties",
            "run_id",
        )
        for command in EXPECTED_ACTION_COMMANDS
    }
    request_paths = {
        (
            "paths",
            f"/v1/action/{command}",
            "post",
            "requestBody",
            "content",
            "application/json",
            "schema",
            "properties",
            "client",
            "properties",
            "request_id",
        )
        for command in EXPECTED_REPLAY_SAFE_COMMANDS
    }
    request_paths.add(
        (
            "components",
            "schemas",
            "ResultEnvelope",
            "properties",
            "data",
            "properties",
            "request_id",
        )
    )
    return run_paths | request_paths


def assert_independent_action_openapi_contract(document: Mapping[str, Any]) -> None:
    """Assert the externally promised inventory independently of the generator."""

    expected_paths = {f"/v1/action/{command}" for command in EXPECTED_ACTION_COMMANDS}
    assert set(document["paths"]) == expected_paths

    consequential = {
        command: document["paths"][f"/v1/action/{command}"]["post"][
            "x-openai-isConsequential"
        ]
        for command in EXPECTED_ACTION_COMMANDS
    }
    assert consequential == EXPECTED_CONSEQUENTIAL

    for command in EXPECTED_ACTION_COMMANDS:
        client = request_schema(document, command)["properties"]["client"]
        if command in EXPECTED_REPLAY_SAFE_COMMANDS:
            assert "request_id" in client["required"]
            assert "request_id" in client["properties"]
        else:
            assert command in EXPECTED_READ_ONLY_COMMANDS
            assert "request_id" not in client["required"]
            assert "request_id" not in client["properties"]

    found = named_run_and_request_id_schemas(document)
    assert set(found) == expected_run_and_request_id_paths()
    for path, schema in found.items():
        for key, expected in EXPECTED_DISH_UUID_SCHEMA.items():
            assert schema.get(key) == expected, (path, key)
