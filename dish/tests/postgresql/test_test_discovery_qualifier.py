from __future__ import annotations

from typing import Any

import pytest

from dish_pg import test_discovery_qualifier as qualifier


GENERATION_ID = "11111111-1111-4111-8111-111111111111"
REGISTRY_ID = "22222222-2222-4222-8222-222222222222"
SECTION_ID = "33333333-3333-4333-8333-333333333333"
TASK_A = "44444444-4444-4444-8444-444444444444"
TASK_B = "55555555-5555-4555-8555-555555555555"


def _environment() -> dict[str, str]:
    return {
        "DISH_PROFILE": "test",
        "DISH_AUTHORITY_BACKEND": "postgresql",
        "DISH_SERVICE_BIND": "127.0.0.1",
        "DISH_SERVICE_PORT": "8765",
        "DISH_SERVICE_AGENT_TOKEN": "secret-token",
        "DISH_PG_DATABASE_URL": "postgresql+psycopg://dish:secret@127.0.0.1/dish_runtime_test",
        "DISH_PG_EXPECTED_DATABASE_NAME": "dish_runtime_test",
        "DISH_PG_EXPECTED_GENERATION_ID": GENERATION_ID,
        "DISH_PG_EXPECTED_SCHEMA_HEAD": "0042_scalar_dish_state",
        "DISH_PG_EXPECTED_RELEASE": "dish-test-release",
        "DISH_PG_AUTHORITY_STATE_DIR": "/home/marco/.local/state/dish/test/pg-authority",
    }


def _sections() -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "sections": [
                {"section_id": SECTION_ID, "section_gid": "1234567890", "name": "Research"}
            ]
        },
    }


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, base_url: str, *, token: str, run_id: str) -> None:
        self.base_url = base_url
        self.token = token
        self.run_id = run_id
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.__class__.instances.append(self)

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "startup_ready": True,
            "backend": "postgresql",
            "profile": "test",
            "identity": {
                "database": "dish_runtime_test",
                "generation_id": GENERATION_ID,
                "schema_head": "0042_scalar_dish_state",
                "dish_release": "dish-test-release",
            },
            "isolation": {"asana_environment_keys": []},
        }

    def execute(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((command, arguments))
        if command == "sections":
            return _sections()
        if command == "section-tasks":
            if "cursor" not in arguments:
                return {
                    "ok": True,
                    "data": {
                        "tasks": [{"dish_id": TASK_A, "section_id": SECTION_ID, "title": "Alpha"}],
                        "next_cursor": "page-2",
                    },
                }
            return {
                "ok": True,
                "data": {
                    "tasks": [{"dish_id": TASK_B, "section_id": SECTION_ID, "title": "Beta"}],
                    "next_cursor": None,
                },
            }
        assert command == "search"
        return {
            "ok": True,
            "data": {"results": [{"dish_id": TASK_A, "section_id": SECTION_ID}]},
        }


def _snapshot(database_url: str) -> qualifier.DatabaseSnapshot:
    assert database_url.endswith("/dish_runtime_test")
    return qualifier.DatabaseSnapshot(
        database="dish_runtime_test",
        generation_id=GENERATION_ID,
        registry_version_id=REGISTRY_ID,
        section_ids=frozenset({SECTION_ID}),
        tasks={TASK_A: SECTION_ID, TASK_B: SECTION_ID},
    )


def test_qualify_uses_cli_and_forces_section_task_pagination() -> None:
    FakeClient.instances.clear()
    cli_calls: list[tuple[str, list[str]]] = []

    def cli_command(*, command: str, arguments: list[str] | tuple[str, ...], **_: str) -> dict[str, Any]:
        cli_calls.append((command, list(arguments)))
        if command == "sections":
            return _sections()
        if command == "section-tasks":
            if "--cursor" not in arguments:
                return {
                    "ok": True,
                    "data": {
                        "tasks": [{"dish_id": TASK_A, "section_id": SECTION_ID}],
                        "next_cursor": "cli-page-2",
                    },
                }
            return {
                "ok": True,
                "data": {
                    "tasks": [{"dish_id": TASK_B, "section_id": SECTION_ID}],
                    "next_cursor": None,
                },
            }
        assert command == "search"
        return {
            "ok": True,
            "data": {"results": [{"dish_id": TASK_A, "section_id": SECTION_ID}]},
        }

    receipt = qualifier.qualify(
        environment=_environment(),
        client_factory=FakeClient,
        cli_command=cli_command,
        database_reader=_snapshot,
    )

    assert receipt["status"] == "PASS"
    assert receipt["observations"] == {
        "sections": 1,
        "tasks": 2,
        "section_task_pages": 2,
        "search_query_source": "first_discovered_title",
        "search_results": 1,
        "cli_sections_match": True,
        "cli_section_tasks_match": True,
        "cli_search_match": True,
        "postgresql_ids_match": True,
    }
    client = FakeClient.instances[-1]
    pages = [arguments for command, arguments in client.calls if command == "section-tasks"]
    assert [page["page_size"] for page in pages] == [1, 1]
    assert "cursor" not in pages[0]
    assert pages[1]["cursor"] == "page-2"
    assert [command for command, _ in client.calls] == ["sections", "section-tasks", "section-tasks", "search"]
    assert [command for command, _ in cli_calls] == [
        "sections",
        "section-tasks",
        "section-tasks",
        "search",
    ]
    assert cli_calls[2][1][-2:] == ["--cursor", "cli-page-2"]
    assert cli_calls[1][1][0] == "1234567890"


def test_qualify_fails_when_search_omits_the_discovered_dish() -> None:
    class EmptySearchClient(FakeClient):
        def execute(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = super().execute(command, arguments)
            if command == "search":
                return {"ok": True, "data": {"results": []}}
            return result

    with pytest.raises(qualifier.QualificationError, match="did not return the discovered title"):
        qualifier.qualify(
            environment=_environment(),
            client_factory=EmptySearchClient,
            cli_command=lambda *, command, **_: (
                _sections()
                if command == "sections"
                else {
                    "ok": True,
                    "data": {
                        "tasks": [
                            {"dish_id": TASK_A, "section_id": SECTION_ID},
                            {"dish_id": TASK_B, "section_id": SECTION_ID},
                        ],
                        "next_cursor": None,
                    },
                }
                if command == "section-tasks"
                else {"ok": True, "data": {"results": []}}
            ),
            database_reader=_snapshot,
        )


def test_qualify_fails_when_service_ids_differ_from_postgresql() -> None:
    def mismatched_snapshot(database_url: str) -> qualifier.DatabaseSnapshot:
        snapshot = _snapshot(database_url)
        return qualifier.DatabaseSnapshot(
            database=snapshot.database,
            generation_id=snapshot.generation_id,
            registry_version_id=snapshot.registry_version_id,
            section_ids=snapshot.section_ids,
            tasks={TASK_A: SECTION_ID},
        )

    with pytest.raises(qualifier.QualificationError, match="section-tasks canonical IDs"):
        qualifier.qualify(
            environment=_environment(),
            client_factory=FakeClient,
            cli_command=lambda *, command, **_: (
                _sections()
                if command == "sections"
                else {
                    "ok": True,
                    "data": {
                        "tasks": [
                            {"dish_id": TASK_A, "section_id": SECTION_ID},
                            {"dish_id": TASK_B, "section_id": SECTION_ID},
                        ],
                        "next_cursor": None,
                    },
                }
                if command == "section-tasks"
                else {
                    "ok": True,
                    "data": {"results": [{"dish_id": TASK_A, "section_id": SECTION_ID}]},
                }
            ),
            database_reader=mismatched_snapshot,
        )


def test_configuration_refuses_non_test_database() -> None:
    environment = _environment()
    environment["DISH_PG_EXPECTED_DATABASE_NAME"] = "dish_runtime_prod"
    with pytest.raises(qualifier.QualificationError, match="outside the configured TEST target"):
        qualifier.qualify(environment=environment)
