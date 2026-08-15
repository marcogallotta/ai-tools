"""TEST dual-stack comparator for PostgreSQL authority vs disposable legacy oracle.

This tooling is intentionally qualification-only. It never mirrors ordinary traffic, provides
failover, or synchronizes the two writable stacks. Curated scenarios are sent explicitly to the
PostgreSQL-authoritative TEST route and a disposable legacy SQLite/Asana oracle route, normalized,
and persisted as durable comparison evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shlex
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy.exc import SQLAlchemyError

from . import stage3_models as workflow_models
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .workflow import WorkflowAuthorityError, WorkflowAuthorityService

PLAN_FORMAT = "dish-test-comparator-plan-v1"
EVIDENCE_FORMAT = "dish-test-comparator-evidence-v1"
DEFAULT_AUTHORITY_ACTION_BASE = "http://127.0.0.1:8786/test"
DEFAULT_ORACLE_ACTION_BASE = "http://127.0.0.1:8786/test-legacy"
DEFAULT_AUTHORITY_HEALTH_URL = "http://127.0.0.1:8765/health"
DEFAULT_ORACLE_HEALTH_URL = "http://127.0.0.1:8795/health"
DEFAULT_EVIDENCE_DIR = Path("/home/marco/.local/state/dish/test/comparator-evidence")
DEFAULT_AUTHORITY_ENV = Path("/home/marco/.config/dish-service/test.env")
DEFAULT_ORACLE_ENV = Path("/home/marco/.config/dish-service/test-legacy.env")
DISPOSABLE_ORACLE_PROJECT_GID = "1216693403164366"
COMPARATOR_RUN_OWNER_ID = "gpt-action"
COMPARATOR_RUN_AGENT = "gpt"
_COMPARATOR_RUN_CAPABILITY_NAMESPACE = "dish-test-comparator-run-v1"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")


class ComparatorError(RuntimeError):
    """Fail-closed comparator configuration or execution error."""


@dataclass(frozen=True)
class TargetConfig:
    name: str
    action_base: str
    health_url: str
    token: str
    env_path: Path
    env: Mapping[str, str]


@dataclass(frozen=True)
class ComparisonOutcome:
    report: Mapping[str, Any]
    evidence_path: Path

    @property
    def mismatch_count(self) -> int:
        return int(self.report.get("mismatch_count", 0))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_env_value(raw: str, *, path: Path, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    try:
        parsed = shlex.split(value, comments=False, posix=True)
    except ValueError as exc:
        raise ComparatorError(f"{path}:{line_number}: invalid environment value") from exc
    if len(parsed) != 1:
        raise ComparatorError(f"{path}:{line_number}: environment value must be one token")
    return parsed[0]


def read_environment_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ComparatorError(f"cannot read environment file {path}: {exc}") from exc
    values: dict[str, str] = {}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not key or any(ch.isspace() for ch in key):
            raise ComparatorError(f"{path}:{line_number}: invalid environment assignment")
        values[key] = _parse_env_value(raw_value, path=path, line_number=line_number)
    return values


def _require(values: Mapping[str, str], key: str, expected: str | None = None) -> str:
    value = str(values.get(key, "")).strip()
    if not value:
        raise ComparatorError(f"environment is missing required {key}")
    if expected is not None and value != expected:
        raise ComparatorError(f"{key} must be {expected!r}, observed {value!r}")
    return value


def _require_state_path(value: str, *, root: Path, field: str) -> None:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ComparatorError(f"{field} must be an absolute path under {root}, observed {raw}")
    canonical_root = root.expanduser().resolve(strict=False)
    observed = raw.resolve(strict=False)
    try:
        observed.relative_to(canonical_root)
    except ValueError as exc:
        raise ComparatorError(
            f"{field} must stay under {canonical_root} after canonicalization, observed {observed}"
        ) from exc


def _database_name_from_url(database_url: str) -> str:
    name = urlsplit(database_url).path.lstrip("/")
    if not name:
        raise ComparatorError("DISH_PG_DATABASE_URL has no database name")
    return name


def validate_target_environments(authority_env: Mapping[str, str], oracle_env: Mapping[str, str]) -> None:
    _require(authority_env, "DISH_AUTHORITY_BACKEND", "postgresql")
    _require(authority_env, "DISH_PROFILE", "test")
    _require(authority_env, "DISH_SERVICE_BIND", "127.0.0.1")
    _require(authority_env, "DISH_ACTION_BIND", "127.0.0.1")
    _require(authority_env, "DISH_SERVICE_PORT", "8765")
    _require(authority_env, "DISH_ACTION_PORT", "8766")
    database_url = _require(authority_env, "DISH_PG_DATABASE_URL")
    expected_database = _require(authority_env, "DISH_PG_EXPECTED_DATABASE_NAME")
    if _database_name_from_url(database_url) != expected_database or not expected_database.endswith("_test"):
        raise ComparatorError("PostgreSQL authority database identity must be the configured _test database")
    _require_state_path(
        _require(authority_env, "DISH_PG_AUTHORITY_STATE_DIR"),
        root=Path("/home/marco/.local/state/dish/test"),
        field="DISH_PG_AUTHORITY_STATE_DIR",
    )
    populated_asana = sorted(key for key, value in authority_env.items() if "ASANA" in key.upper() and value.strip())
    if populated_asana:
        raise ComparatorError(
            "PostgreSQL TEST authority must have no populated Asana environment keys: " + ", ".join(populated_asana)
        )
    if authority_env.get("DISH_DARK_LAUNCH_MODE", "off").strip().lower() not in {"", "off"}:
        raise ComparatorError("PostgreSQL TEST authority must keep dark launch off")

    _require(oracle_env, "DISH_AUTHORITY_BACKEND", "legacy")
    _require(oracle_env, "DISH_TEST_COMPARATOR_DISPOSABLE", "1")
    _require(oracle_env, "DISH_SERVICE_BIND", "127.0.0.1")
    _require(oracle_env, "DISH_ACTION_BIND", "127.0.0.1")
    _require(oracle_env, "DISH_SERVICE_PORT", "8795")
    _require(oracle_env, "DISH_ACTION_PORT", "8796")
    _require(oracle_env, "DISH_COOKING_PROJECT_GID", DISPOSABLE_ORACLE_PROJECT_GID)
    _require_state_path(
        _require(oracle_env, "DISH_DB_PATH"),
        root=Path("/home/marco/.local/state/dish/test-legacy"),
        field="DISH_DB_PATH",
    )
    _require_state_path(
        _require(oracle_env, "DISH_SERVICE_BACKUP_DIR"),
        root=Path("/home/marco/.local/state/dish/test-legacy"),
        field="DISH_SERVICE_BACKUP_DIR",
    )
    if not any(oracle_env.get(key, "").strip() for key in ("ASANA_ENV", "ASANA_PAT")):
        raise ComparatorError("legacy comparator must have an explicit Asana credential source")
    if oracle_env.get("DISH_DARK_LAUNCH_MODE", "off").strip().lower() != "off":
        raise ComparatorError("legacy comparator must keep dark launch off; oracle state must not synchronize to PG")
    if _require(authority_env, "DISH_SERVICE_ACTION_TOKEN") == _require(oracle_env, "DISH_SERVICE_ACTION_TOKEN"):
        raise ComparatorError("authority and oracle Action tokens must be distinct")


def load_targets(
    *,
    authority_env_path: Path = DEFAULT_AUTHORITY_ENV,
    oracle_env_path: Path = DEFAULT_ORACLE_ENV,
    authority_action_base: str = DEFAULT_AUTHORITY_ACTION_BASE,
    oracle_action_base: str = DEFAULT_ORACLE_ACTION_BASE,
    authority_health_url: str = DEFAULT_AUTHORITY_HEALTH_URL,
    oracle_health_url: str = DEFAULT_ORACLE_HEALTH_URL,
) -> tuple[TargetConfig, TargetConfig]:
    authority_env = read_environment_file(authority_env_path)
    oracle_env = read_environment_file(oracle_env_path)
    validate_target_environments(authority_env, oracle_env)
    return (
        TargetConfig("authority", authority_action_base.rstrip("/"), authority_health_url, _require(authority_env, "DISH_SERVICE_ACTION_TOKEN"), authority_env_path, authority_env),
        TargetConfig("oracle", oracle_action_base.rstrip("/"), oracle_health_url, _require(oracle_env, "DISH_SERVICE_ACTION_TOKEN"), oracle_env_path, oracle_env),
    )


def _mutating_authority_generation_id(
    authority: TargetConfig, *, preflight: Mapping[str, Any]
) -> uuid.UUID:
    """Bind direct run registration to the exact TEST authority proved by preflight."""

    _require(authority.env, "DISH_AUTHORITY_BACKEND", "postgresql")
    _require(authority.env, "DISH_PROFILE", "test")
    database_url = _require(authority.env, "DISH_PG_DATABASE_URL")
    expected_database = _require(authority.env, "DISH_PG_EXPECTED_DATABASE_NAME")
    if _database_name_from_url(database_url) != expected_database or not expected_database.endswith("_test"):
        raise ComparatorError("PostgreSQL comparator run registration requires the configured _test database")

    expected_generation_text = _require(authority.env, "DISH_PG_EXPECTED_GENERATION_ID")
    try:
        expected_generation_id = uuid.UUID(expected_generation_text)
    except ValueError as exc:
        raise ComparatorError("DISH_PG_EXPECTED_GENERATION_ID must be a UUID") from exc

    authority_health = preflight.get("authority_health")
    identity = authority_health.get("identity") if isinstance(authority_health, Mapping) else None
    if not isinstance(identity, Mapping):
        raise ComparatorError("PostgreSQL TEST health preflight did not expose authority identity")
    if identity.get("database") != expected_database:
        raise ComparatorError("PostgreSQL TEST health database does not match comparator authority environment")
    if identity.get("generation_status") != "active":
        raise ComparatorError("PostgreSQL TEST health generation is not active")
    if str(identity.get("generation_id", "")) != str(expected_generation_id):
        raise ComparatorError("PostgreSQL TEST health generation does not match comparator authority environment")
    return expected_generation_id


def _comparator_run_capability_digest(*, generation_id: uuid.UUID, run_id: uuid.UUID) -> bytes:
    material = "\0".join(
        (
            _COMPARATOR_RUN_CAPABILITY_NAMESPACE,
            str(generation_id),
            COMPARATOR_RUN_OWNER_ID,
            COMPARATOR_RUN_AGENT,
            str(run_id),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).digest()


def _register_mutating_authority_run(
    authority: TargetConfig,
    *,
    preflight: Mapping[str, Any],
    run_id: uuid.UUID,
    registered_at: datetime,
) -> Mapping[str, Any]:
    """Register one comparator Action run through canonical PostgreSQL workflow authority."""

    generation_id = _mutating_authority_generation_id(authority, preflight=preflight)
    capability_digest = _comparator_run_capability_digest(
        generation_id=generation_id,
        run_id=run_id,
    )
    engine = create_database_engine(
        DatabaseSettings(url=_require(authority.env, "DISH_PG_DATABASE_URL"))
    )
    created = False
    try:
        with session_scope(session_factory(engine)) as session:
            existing = session.get(workflow_models.ServiceRun, run_id)
            if existing is None:
                WorkflowAuthorityService(session).register_run(
                    run_id=run_id,
                    generation_id=generation_id,
                    owner_id=COMPARATOR_RUN_OWNER_ID,
                    agent=COMPARATOR_RUN_AGENT,
                    capability_digest=capability_digest,
                    registered_at=registered_at,
                )
                created = True
            elif (
                existing.generation_id != generation_id
                or existing.owner_id != COMPARATOR_RUN_OWNER_ID
                or existing.agent != COMPARATOR_RUN_AGENT
                or existing.capability_digest != capability_digest
                or existing.status != "active"
            ):
                raise ComparatorError(
                    "existing comparator run identity is not an active run for the expected TEST generation"
                )
            WorkflowAuthorityService(session).repo.require_active_run(
                generation_id=generation_id,
                run_id=run_id,
                owner_id=COMPARATOR_RUN_OWNER_ID,
            )
    except ComparatorError:
        raise
    except WorkflowAuthorityError as exc:
        raise ComparatorError(
            f"PostgreSQL TEST comparator run registration was rejected: {exc}"
        ) from exc
    except SQLAlchemyError as exc:
        raise ComparatorError(
            "PostgreSQL TEST comparator run registration could not reach workflow authority "
            f"({type(exc).__name__})"
        ) from exc
    finally:
        engine.dispose()

    return {
        "mechanism": "WorkflowAuthorityService.register_run",
        "generation_id": str(generation_id),
        "run_id": str(run_id),
        "owner_id": COMPARATOR_RUN_OWNER_ID,
        "agent": COMPARATOR_RUN_AGENT,
        "created": created,
    }


def _request_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: Mapping[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ComparatorError(f"{method} {url} failed: {type(exc).__name__}: {exc}") from exc
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComparatorError(f"{method} {url} returned non-JSON HTTP {status}") from exc


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ComparatorError(f"JSON pointer must start with '/': {pointer!r}")
    current = value
    for raw_part in pointer.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise ComparatorError(f"response does not contain JSON pointer {pointer!r}") from exc
        else:
            raise ComparatorError(f"response does not contain JSON pointer {pointer!r}")
    return current


def _render_template(value: Any, bindings: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        exact = _TEMPLATE_RE.fullmatch(value)
        if exact:
            key = exact.group(1)
            if key not in bindings:
                raise ComparatorError(f"unknown template binding {key!r}")
            return copy.deepcopy(bindings[key])
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in bindings:
                raise ComparatorError(f"unknown template binding {key!r}")
            return str(bindings[key])
        return _TEMPLATE_RE.sub(replace, value)
    if isinstance(value, list):
        return [_render_template(item, bindings) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _render_template(item, bindings) for key, item in value.items()}
    return copy.deepcopy(value)


def normalize_value(
    value: Any,
    *,
    drop_keys: frozenset[str] = frozenset(),
    rename_keys: Mapping[str, str] | None = None,
    identity_aliases: Mapping[str, str] | None = None,
) -> Any:
    renames = dict(rename_keys or {})
    aliases = dict(identity_aliases or {})
    if isinstance(value, str):
        if value in aliases:
            return aliases[value]
        lowered = value.lower()
        if _UUID_RE.fullmatch(lowered):
            return "<uuid>"
        if _TIMESTAMP_RE.fullmatch(value):
            return "<timestamp>"
        return value
    if isinstance(value, list):
        return [normalize_value(item, drop_keys=drop_keys, rename_keys=renames, identity_aliases=aliases) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if key in drop_keys:
                continue
            target_key = renames.get(key, key)
            normalized[target_key] = normalize_value(value[key], drop_keys=drop_keys, rename_keys=renames, identity_aliases=aliases)
        return normalized
    return value


def load_plan(path: Path) -> Mapping[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparatorError(f"cannot load comparator plan {path}: {exc}") from exc
    if not isinstance(plan, Mapping) or plan.get("format") != PLAN_FORMAT:
        raise ComparatorError(f"comparator plan must use format {PLAN_FORMAT}")
    scenarios = plan.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ComparatorError("comparator plan must contain a non-empty scenarios list")
    seen: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            raise ComparatorError("each comparator scenario must be an object")
        scenario_id = str(scenario.get("id", "")).strip()
        command = str(scenario.get("command", "")).strip()
        if not scenario_id or not command:
            raise ComparatorError("each comparator scenario needs id and command")
        if scenario_id in seen:
            raise ComparatorError(f"duplicate comparator scenario id {scenario_id!r}")
        seen.add(scenario_id)
        if not isinstance(scenario.get("mutating", False), bool):
            raise ComparatorError(f"scenario {scenario_id!r} mutating must be boolean")
    return plan


def _command_request(target: TargetConfig, *, command: str, arguments: Mapping[str, Any], run_id: str, request_id: str | None) -> Mapping[str, Any]:
    client: dict[str, str] = {"run_id": run_id}
    if request_id is not None:
        client["request_id"] = request_id
    status, response = _request_json(
        f"{target.action_base}/v1/action/{command}",
        method="POST",
        token=target.token,
        payload={"client": client, "arguments": dict(arguments)},
    )
    if status != 200 or not isinstance(response, Mapping):
        raise ComparatorError(f"{target.name} scenario command {command!r} returned HTTP {status}")
    return response


def _route_preflight(authority: TargetConfig, oracle: TargetConfig, *, run_id: str) -> Mapping[str, Any]:
    authority_status, authority_health = _request_json(authority.health_url)
    oracle_status, oracle_health = _request_json(oracle.health_url)
    if authority_status != 200 or not isinstance(authority_health, Mapping) or authority_health.get("ok") is not True:
        raise ComparatorError("PostgreSQL authority health preflight is not ready")
    if authority_health.get("backend") != "postgresql" or authority_health.get("profile") != "test":
        raise ComparatorError("default TEST private listener is not PostgreSQL-authoritative TEST")
    isolation = authority_health.get("isolation")
    if not isinstance(isolation, Mapping) or isolation.get("asana_environment_keys") not in ([], ()):
        raise ComparatorError("PostgreSQL TEST authority health does not prove an empty Asana environment")
    if oracle_status != 200 or not isinstance(oracle_health, Mapping) or oracle_health.get("ok") is not True:
        raise ComparatorError("legacy comparator health preflight is not ready")
    if not isinstance(oracle_health.get("asana"), Mapping) or oracle_health["asana"].get("ok") is not True:
        raise ComparatorError("legacy comparator health does not prove its isolated Asana backend is ready")

    authority_doc_status, authority_doc = _request_json(f"{authority.action_base}/openapi/action.json")
    oracle_doc_status, oracle_doc = _request_json(f"{oracle.action_base}/openapi/action.json")
    if authority_doc_status != 200 or oracle_doc_status != 200 or not isinstance(authority_doc, Mapping) or not isinstance(oracle_doc, Mapping):
        raise ComparatorError("Action OpenAPI preflight failed")
    authority_paths = sorted(str(path) for path in authority_doc.get("paths", {}))
    oracle_paths = sorted(str(path) for path in oracle_doc.get("paths", {}))

    authority_sections = _command_request(authority, command="sections", arguments={"agent": "gpt"}, run_id=run_id, request_id=None)
    oracle_sections = _command_request(oracle, command="sections", arguments={"agent": "gpt"}, run_id=run_id, request_id=None)
    authority_data = authority_sections.get("data", {})
    oracle_data = oracle_sections.get("data", {})
    authority_project_gid = authority_data.get("project_gid") if isinstance(authority_data, Mapping) else None
    oracle_project_gid = oracle_data.get("project_gid") if isinstance(oracle_data, Mapping) else None
    expected_oracle_project_gid = DISPOSABLE_ORACLE_PROJECT_GID
    configured_oracle_project_gid = str(oracle.env.get("DISH_COOKING_PROJECT_GID", "")).strip()
    if authority_project_gid is not None:
        raise ComparatorError("default TEST Action route returned legacy project identity; refusing comparator mutations")
    if configured_oracle_project_gid != expected_oracle_project_gid:
        raise ComparatorError(
            "legacy comparator environment is not bound to the repository-designated disposable TEST project"
        )
    if str(oracle_project_gid or "") != expected_oracle_project_gid:
        raise ComparatorError(
            "legacy comparator Action route is not bound to the repository-designated disposable TEST project"
        )

    return {
        "authority_health": {"backend": authority_health.get("backend"), "profile": authority_health.get("profile"), "startup_ready": authority_health.get("startup_ready"), "identity": authority_health.get("identity"), "isolation": isolation},
        "oracle_health": {"service": oracle_health.get("service"), "configuration": oracle_health.get("configuration"), "database": oracle_health.get("database"), "asana": oracle_health.get("asana")},
        "action_contract": {"authority_paths": authority_paths, "oracle_paths": oracle_paths, "match": authority_paths == oracle_paths},
        "routing": {"authority_action_base": authority.action_base, "oracle_action_base": oracle.action_base, "authority_project_gid_present": False, "oracle_project_gid": expected_oracle_project_gid},
    }


def _scenario_arguments(scenario: Mapping[str, Any], *, target: str, bindings: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = scenario.get(f"{target}_arguments", scenario.get("arguments", {}))
    if not isinstance(raw, Mapping):
        raise ComparatorError(f"scenario {scenario['id']!r} arguments must be objects")
    rendered = _render_template(raw, bindings)
    assert isinstance(rendered, Mapping)
    return rendered


def _capture_response_values(scenario: Mapping[str, Any], *, target: str, response: Mapping[str, Any], target_bindings: dict[str, Any], identity_aliases: dict[str, str]) -> None:
    captures = scenario.get("captures", {})
    if not isinstance(captures, Mapping):
        raise ComparatorError(f"scenario {scenario['id']!r} captures must be an object")
    for logical_name, paths in captures.items():
        if not isinstance(paths, Mapping) or target not in paths:
            raise ComparatorError(f"scenario {scenario['id']!r} capture {logical_name!r} needs {target} pointer")
        captured = _json_pointer(response, str(paths[target]))
        target_bindings[str(logical_name)] = captured
        if isinstance(captured, str):
            identity_aliases[captured] = f"<identity:{logical_name}>"


def _project_response(response: Mapping[str, Any], *, target: str, projection: Mapping[str, Any]) -> Mapping[str, Any]:
    projected: dict[str, Any] = {}
    for logical_field, pointer_spec in projection.items():
        if isinstance(pointer_spec, str):
            pointer = pointer_spec
        elif isinstance(pointer_spec, Mapping) and target in pointer_spec:
            pointer = str(pointer_spec[target])
        else:
            raise ComparatorError(f"projection {logical_field!r} has no pointer for {target}")
        projected[str(logical_field)] = _json_pointer(response, pointer)
    return projected


def _normalized_comparison(scenario: Mapping[str, Any], *, authority_response: Mapping[str, Any], oracle_response: Mapping[str, Any], identity_aliases: Mapping[str, str]) -> tuple[Any, Any]:
    comparison = scenario.get("compare", {})
    if not isinstance(comparison, Mapping):
        raise ComparatorError(f"scenario {scenario['id']!r} compare must be an object")
    projection = comparison.get("projection")
    if projection is not None:
        if not isinstance(projection, Mapping):
            raise ComparatorError(f"scenario {scenario['id']!r} projection must be an object")
        authority_value: Any = _project_response(authority_response, target="authority", projection=projection)
        oracle_value: Any = _project_response(oracle_response, target="oracle", projection=projection)
    else:
        authority_value = authority_response
        oracle_value = oracle_response
    raw_drop = comparison.get("drop_keys", [])
    raw_rename = comparison.get("rename_keys", {})
    if not isinstance(raw_drop, list) or not all(isinstance(key, str) for key in raw_drop):
        raise ComparatorError(f"scenario {scenario['id']!r} drop_keys must be a string list")
    if not isinstance(raw_rename, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_rename.items()):
        raise ComparatorError(f"scenario {scenario['id']!r} rename_keys must be a string map")
    kwargs = {
        "drop_keys": frozenset(raw_drop),
        "rename_keys": {str(key): str(value) for key, value in raw_rename.items()},
        "identity_aliases": identity_aliases,
    }
    return normalize_value(authority_value, **kwargs), normalize_value(oracle_value, **kwargs)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ComparatorError(f"cannot persist comparator evidence {path}: {exc}") from exc


def run_comparison(
    *,
    plan: Mapping[str, Any],
    authority: TargetConfig,
    oracle: TargetConfig,
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    allow_mutating_scenarios: bool = False,
    now: datetime | None = None,
    run_id: uuid.UUID | None = None,
) -> ComparisonOutcome:
    started_at = now or _utc_now()
    comparison_run_id = run_id or uuid.uuid4()
    run_id_text = str(comparison_run_id)
    preflight = _route_preflight(authority, oracle, run_id=run_id_text)
    has_mutating_scenario = any(
        bool(scenario.get("mutating", False))
        for scenario in plan["scenarios"]
        if isinstance(scenario, Mapping)
    )
    run_authority = None
    if allow_mutating_scenarios and has_mutating_scenario:
        run_authority = _register_mutating_authority_run(
            authority,
            preflight=preflight,
            run_id=comparison_run_id,
            registered_at=started_at,
        )
    target_bindings: dict[str, dict[str, Any]] = {"authority": {"run_id": run_id_text}, "oracle": {"run_id": run_id_text}}
    identity_aliases: dict[str, str] = {run_id_text: "<run_id>"}
    results: list[dict[str, Any]] = []
    mismatches = 0
    skipped = 0
    completed_ids: set[str] = set()

    for scenario in plan["scenarios"]:
        assert isinstance(scenario, Mapping)
        scenario_id = str(scenario["id"])
        dependencies = scenario.get("requires", [])
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            raise ComparatorError(f"scenario {scenario_id!r} requires must be a string list")
        missing_dependencies = [item for item in dependencies if item not in completed_ids]
        mutating = bool(scenario.get("mutating", False))
        if missing_dependencies or (mutating and not allow_mutating_scenarios):
            skipped += 1
            reason = "dependency_not_run:" + ",".join(missing_dependencies) if missing_dependencies else "mutating_scenarios_not_enabled"
            results.append({"id": scenario_id, "command": scenario["command"], "mutating": mutating, "status": "skipped", "reason": reason})
            continue

        command = str(scenario["command"])
        request_id = str(uuid.uuid5(comparison_run_id, scenario_id)) if mutating else None
        if request_id is not None:
            identity_aliases[request_id] = f"<request:{scenario_id}>"
        responses: dict[str, Mapping[str, Any]] = {}
        for target in (authority, oracle):
            arguments = _scenario_arguments(scenario, target=target.name, bindings=target_bindings[target.name])
            response = _command_request(target, command=command, arguments=arguments, run_id=run_id_text, request_id=request_id)
            responses[target.name] = response
            _capture_response_values(scenario, target=target.name, response=response, target_bindings=target_bindings[target.name], identity_aliases=identity_aliases)
        authority_normalized, oracle_normalized = _normalized_comparison(scenario, authority_response=responses["authority"], oracle_response=responses["oracle"], identity_aliases=identity_aliases)
        match = authority_normalized == oracle_normalized
        if not match:
            mismatches += 1
        results.append({"id": scenario_id, "command": command, "mutating": mutating, "status": "match" if match else "mismatch", "authority": authority_normalized, "oracle": oracle_normalized})
        completed_ids.add(scenario_id)

    if not preflight["action_contract"]["match"]:
        mismatches += 1
    full_qualification = allow_mutating_scenarios and skipped == 0
    qualification_passed = full_qualification and mismatches == 0
    report: dict[str, Any] = {
        "format": EVIDENCE_FORMAT,
        "plan_format": PLAN_FORMAT,
        "plan_name": plan.get("name"),
        "comparison_run_id": run_id_text,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": _utc_now().isoformat().replace("+00:00", "Z"),
        "status": "match" if mismatches == 0 else "mismatch",
        "mismatch_count": mismatches,
        "skipped_count": skipped,
        "full_qualification": full_qualification,
        "qualification_passed": qualification_passed,
        "mutating_scenarios_enabled": allow_mutating_scenarios,
        "preflight": normalize_value(preflight, identity_aliases=identity_aliases),
        "scenarios": results,
        "environment": {
            "authority_env": str(authority.env_path),
            "oracle_env": str(oracle.env_path),
            "authority_backend": authority.env.get("DISH_AUTHORITY_BACKEND"),
            "oracle_backend": oracle.env.get("DISH_AUTHORITY_BACKEND"),
            "oracle_disposable": oracle.env.get("DISH_TEST_COMPARATOR_DISPOSABLE") == "1",
        },
    }
    if run_authority is not None:
        report["run_authority"] = normalize_value(
            run_authority, identity_aliases=identity_aliases
        )
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    evidence_path = evidence_dir / f"comparison-{stamp}-{run_id_text}.json"
    _atomic_write_json(evidence_path, report)
    _atomic_write_json(evidence_dir / "latest.json", report)
    return ComparisonOutcome(report=report, evidence_path=evidence_path)
