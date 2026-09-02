"""Small live TEST discovery qualifier over the existing Dish read surfaces."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, text

from dish_service.client import DishServiceClient

from . import models
from .database import DatabaseSettings, create_database_engine, session_factory
from .test_comparator import read_environment_file


TEST_ENV_PATH = Path("/home/marco/.config/dish-service/test.env")
TEST_STATE_ROOT = Path("/home/marco/.local/state/dish/test")


class QualificationError(RuntimeError):
    """The live TEST read surfaces do not agree with PostgreSQL authority."""


@dataclass(frozen=True)
class DatabaseSnapshot:
    database: str
    generation_id: str
    registry_version_id: str
    section_ids: frozenset[str]
    tasks: Mapping[str, str]


def _required(environment: Mapping[str, str], name: str) -> str:
    value = str(environment.get(name, "")).strip()
    if not value:
        raise QualificationError(f"TEST environment is missing {name}")
    return value


def _test_configuration(environment: Mapping[str, str]) -> tuple[str, str, str]:
    if _required(environment, "DISH_PROFILE") != "test":
        raise QualificationError("refusing a non-TEST profile")
    if _required(environment, "DISH_AUTHORITY_BACKEND") != "postgresql":
        raise QualificationError("TEST authority is not PostgreSQL")
    bind = _required(environment, "DISH_SERVICE_BIND")
    port = _required(environment, "DISH_SERVICE_PORT")
    if bind not in {"127.0.0.1", "localhost"} or port != "8765":
        raise QualificationError("TEST service is not on the expected loopback listener")
    populated_asana = [
        key for key, value in environment.items() if "ASANA" in key.upper() and value.strip()
    ]
    if populated_asana:
        raise QualificationError("PostgreSQL TEST environment contains Asana configuration")
    database_url = _required(environment, "DISH_PG_DATABASE_URL")
    expected_database = _required(environment, "DISH_PG_EXPECTED_DATABASE_NAME")
    if urlsplit(database_url).path.lstrip("/") != expected_database or not expected_database.endswith("_test"):
        raise QualificationError("refusing a database outside the configured TEST target")
    state_root = Path(_required(environment, "DISH_PG_AUTHORITY_STATE_DIR")).resolve(strict=False)
    try:
        state_root.relative_to(TEST_STATE_ROOT)
    except ValueError as exc:
        raise QualificationError("TEST authority state is outside the TEST root") from exc
    return f"http://{bind}:{port}", database_url, expected_database


def read_database_snapshot(database_url: str) -> DatabaseSnapshot:
    """Read canonical discovery identities in an explicitly read-only transaction."""

    engine = create_database_engine(DatabaseSettings(url=database_url))
    maker = session_factory(engine)
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            session = maker(bind=connection)
            try:
                database = str(session.scalar(text("SELECT current_database()")))
                generation = session.scalar(
                    select(models.AuthorityGeneration).where(
                        models.AuthorityGeneration.status == "active"
                    )
                )
                if generation is None:
                    raise QualificationError("TEST PostgreSQL has no active generation")
                registry = session.get(models.ActiveSectionRegistry, generation.generation_id)
                if registry is None:
                    raise QualificationError("TEST PostgreSQL has no active section registry")
                section_ids = frozenset(
                    str(value)
                    for value in session.scalars(
                        select(models.SectionRegistryEntry.section_id).where(
                            models.SectionRegistryEntry.registry_version_id
                            == registry.registry_version_id
                        )
                    )
                )
                task_rows = session.execute(
                    select(models.DishState.task_id, models.DishState.section_id)
                    .join(models.DishTask, models.DishTask.task_id == models.DishState.task_id)
                    .where(
                        models.DishState.generation_id == generation.generation_id,
                        models.DishState.registry_version_id == registry.registry_version_id,
                        models.DishState.completed.is_(False),
                        models.DishState.archived_at.is_(None),
                        models.DishTask.existence_state != "retired",
                    )
                )
                tasks = {str(task_id): str(section_id) for task_id, section_id in task_rows}
                return DatabaseSnapshot(
                    database=database,
                    generation_id=str(generation.generation_id),
                    registry_version_id=str(registry.registry_version_id),
                    section_ids=section_ids,
                    tasks=tasks,
                )
            finally:
                session.close()
    finally:
        engine.dispose()


def run_cli_command(
    *, command: str, arguments: Sequence[str], base_url: str, token: str, run_id: str
) -> Mapping[str, Any]:
    environment = dict(os.environ)
    environment.update(
        {
            "DISH_MODE": "service",
            "DISH_PROFILE": "test",
            "DISH_SERVICE_URL_TEST": base_url,
            "DISH_SERVICE_TOKEN_TEST": token,
            "DISH_CLIENT_RUN_ID": run_id,
        }
    )
    completed = subprocess.run(
        ["dish", "--profile", "test", command, *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        raise QualificationError(f"Dish CLI {command} call failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise QualificationError(f"Dish CLI {command} returned non-JSON output") from exc
    if not isinstance(result, Mapping):
        raise QualificationError(f"Dish CLI {command} returned a non-object result")
    return result


def _data(result: Mapping[str, Any], command: str) -> Mapping[str, Any]:
    if result.get("ok") is not True:
        raise QualificationError(f"{command} did not return OK")
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise QualificationError(f"{command} omitted result data")
    return data


def _sections_by_id(data: Mapping[str, Any]) -> dict[str, str | None]:
    sections = data.get("sections")
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        raise QualificationError("sections returned malformed data")
    try:
        return {
            str(item["section_id"]): (
                str(item["section_gid"]) if item.get("section_gid") is not None else None
            )
            for item in sections
        }
    except (KeyError, TypeError) as exc:
        raise QualificationError("sections omitted canonical section identity") from exc


def qualify(
    *,
    environment: Mapping[str, str],
    client_factory: Callable[..., Any] = DishServiceClient,
    cli_command: Callable[..., Mapping[str, Any]] = run_cli_command,
    database_reader: Callable[[str], DatabaseSnapshot] = read_database_snapshot,
) -> dict[str, Any]:
    base_url, database_url, expected_database = _test_configuration(environment)
    run_id = str(uuid.uuid4())
    token = _required(environment, "DISH_SERVICE_AGENT_TOKEN")
    client = client_factory(base_url, token=token, run_id=run_id)
    health = client.health()
    identity = health.get("identity") if isinstance(health, Mapping) else None
    if (
        health.get("ok") is not True
        or health.get("startup_ready") is not True
        or health.get("backend") != "postgresql"
        or health.get("profile") != "test"
        or not isinstance(identity, Mapping)
        or identity.get("database") != expected_database
        or identity.get("generation_id") != _required(environment, "DISH_PG_EXPECTED_GENERATION_ID")
        or identity.get("schema_head") != _required(environment, "DISH_PG_EXPECTED_SCHEMA_HEAD")
        or identity.get("dish_release") != _required(environment, "DISH_PG_EXPECTED_RELEASE")
        or health.get("isolation", {}).get("asana_environment_keys") not in ([], ())
    ):
        raise QualificationError("service health does not prove the configured PostgreSQL TEST identity")

    client_sections = _data(client.execute("sections", {"agent": "codex"}), "client sections")
    cli_result = cli_command(
        command="sections",
        arguments=("--agent", "codex"),
        base_url=base_url,
        token=token,
        run_id=run_id,
    )
    cli_data = _data(cli_result, "CLI sections")
    client_section_aliases = _sections_by_id(client_sections)
    client_section_ids = frozenset(client_section_aliases)
    if frozenset(_sections_by_id(cli_data)) != client_section_ids:
        raise QualificationError("client and CLI sections canonical IDs differ")

    discovered_tasks: dict[str, str] = {}
    cli_tasks: dict[str, str] = {}
    first_search_target: tuple[str, str] | None = None
    page_count = 0
    for section_id in sorted(client_section_ids):
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            arguments: dict[str, Any] = {
                "agent": "codex",
                "section_id": section_id,
                "page_size": 1,
            }
            if cursor is not None:
                arguments["cursor"] = cursor
            page = _data(client.execute("section-tasks", arguments), "section-tasks")
            page_count += 1
            tasks = page.get("tasks")
            if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
                raise QualificationError("section-tasks returned malformed tasks")
            for task in tasks:
                try:
                    dish_id = str(task["dish_id"])
                    observed_section = str(task["section_id"])
                    title = str(task["title"])
                except (KeyError, TypeError) as exc:
                    raise QualificationError("section-tasks omitted canonical IDs") from exc
                if observed_section != section_id or dish_id in discovered_tasks:
                    raise QualificationError("section-tasks returned inconsistent canonical IDs")
                discovered_tasks[dish_id] = observed_section
                first_search_target = first_search_target or (dish_id, title)
            next_cursor = page.get("next_cursor")
            if next_cursor is None:
                break
            cursor = str(next_cursor)
            if not cursor or cursor in seen_cursors:
                raise QualificationError("section-tasks pagination did not advance")
            seen_cursors.add(cursor)

        cli_cursor: str | None = None
        cli_seen_cursors: set[str] = set()
        while True:
            section_gid = client_section_aliases[section_id]
            if section_gid is None or not section_gid.isdigit():
                raise QualificationError(
                    "sections omitted the numeric compatibility ID required by the CLI"
                )
            cli_arguments = [section_gid, "--agent", "codex"]
            if cli_cursor is not None:
                cli_arguments.extend(("--cursor", cli_cursor))
            cli_page = _data(
                cli_command(
                    command="section-tasks",
                    arguments=cli_arguments,
                    base_url=base_url,
                    token=token,
                    run_id=run_id,
                ),
                "CLI section-tasks",
            )
            cli_page_tasks = cli_page.get("tasks")
            if not isinstance(cli_page_tasks, Sequence) or isinstance(
                cli_page_tasks, (str, bytes)
            ):
                raise QualificationError("CLI section-tasks returned malformed tasks")
            for task in cli_page_tasks:
                try:
                    dish_id = str(task["dish_id"])
                    observed_section = str(task["section_id"])
                except (KeyError, TypeError) as exc:
                    raise QualificationError("CLI section-tasks omitted canonical IDs") from exc
                if observed_section != section_id or dish_id in cli_tasks:
                    raise QualificationError("CLI section-tasks returned inconsistent canonical IDs")
                cli_tasks[dish_id] = observed_section
            cli_next_cursor = cli_page.get("next_cursor")
            if cli_next_cursor is None:
                break
            cli_cursor = str(cli_next_cursor)
            if not cli_cursor or cli_cursor in cli_seen_cursors:
                raise QualificationError("CLI section-tasks pagination did not advance")
            cli_seen_cursors.add(cli_cursor)

    if cli_tasks != discovered_tasks:
        raise QualificationError("client and CLI section-tasks canonical IDs differ")

    query = first_search_target[1][:64] if first_search_target else "dish"
    search_data = _data(
        client.execute("search", {"agent": "codex", "query": query}), "search"
    )
    results = search_data.get("results")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise QualificationError("search returned malformed results")
    search_tasks = {
        str(item["dish_id"]): str(item["section_id"])
        for item in results
        if isinstance(item, Mapping)
    }
    if (
        first_search_target is not None
        and search_tasks.get(first_search_target[0])
        != discovered_tasks[first_search_target[0]]
    ):
        raise QualificationError("search did not return the discovered title's canonical Dish")
    cli_search = _data(
        cli_command(
            command="search",
            arguments=(query, "--agent", "codex"),
            base_url=base_url,
            token=token,
            run_id=run_id,
        ),
        "CLI search",
    )
    cli_search_results = cli_search.get("results")
    if not isinstance(cli_search_results, Sequence) or isinstance(
        cli_search_results, (str, bytes)
    ):
        raise QualificationError("CLI search returned malformed results")
    cli_search_tasks = {
        str(item["dish_id"]): str(item["section_id"])
        for item in cli_search_results
        if isinstance(item, Mapping)
    }
    if cli_search_tasks != search_tasks:
        raise QualificationError("client and CLI search canonical IDs differ")
    if (
        first_search_target is not None
        and cli_search_tasks.get(first_search_target[0])
        != discovered_tasks[first_search_target[0]]
    ):
        raise QualificationError("CLI search did not return the discovered title's canonical Dish")

    snapshot = database_reader(database_url)
    if snapshot.database != expected_database or snapshot.generation_id != str(identity["generation_id"]):
        raise QualificationError("service and read-only PostgreSQL identities differ")
    if snapshot.section_ids != client_section_ids:
        raise QualificationError("sections canonical IDs differ from PostgreSQL")
    if dict(snapshot.tasks) != discovered_tasks:
        raise QualificationError("section-tasks canonical IDs differ from PostgreSQL")
    if any(snapshot.tasks.get(task_id) != section_id for task_id, section_id in search_tasks.items()):
        raise QualificationError("search canonical IDs differ from PostgreSQL")

    return {
        "format": "dish-test-discovery-qualification-v1",
        "status": "PASS",
        "target": {"profile": "test", "backend": "postgresql", "database": snapshot.database},
        "identity": {
            "generation_id": snapshot.generation_id,
            "registry_version_id": snapshot.registry_version_id,
        },
        "observations": {
            "sections": len(client_section_ids),
            "tasks": len(discovered_tasks),
            "section_task_pages": page_count,
            "search_query_source": (
                "first_discovered_title" if first_search_target else "empty-corpus fallback"
            ),
            "search_results": len(search_tasks),
            "cli_sections_match": True,
            "cli_section_tasks_match": True,
            "cli_search_match": True,
            "postgresql_ids_match": True,
        },
    }


def main() -> int:
    environment: Mapping[str, str] = {}
    try:
        environment = read_environment_file(TEST_ENV_PATH)
        receipt = qualify(environment=environment)
    except Exception as exc:
        message = str(exc)
        for name, value in environment.items():
            if any(marker in name.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "DATABASE_URL")):
                if value:
                    message = message.replace(value, "<redacted>")
        receipt = {
            "format": "dish-test-discovery-qualification-v1",
            "status": "FAIL",
            "error": {"type": type(exc).__name__, "message": message},
        }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if receipt["status"] == "PASS" else 1
