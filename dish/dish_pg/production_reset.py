"""Reviewed destructive reset machinery for the production PostgreSQL target.

This module deliberately separates three phases:

1. snapshot the exact database identity and access state that a drop would erase;
2. fence connections, terminate blockers, and drop/recreate the database; and
3. after the caller has rebuilt the schema/data, restore and verify access atomically.

The operator-facing entrypoint is ``scripts/dish-pg-production-reset``.  Keeping
this logic importable lets native PostgreSQL tests exercise the same DDL and ACL
paths without ever targeting production.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, URL, make_url


class ProductionResetError(RuntimeError):
    """The destructive reset cannot proceed or its access state cannot be restored."""


@dataclass(frozen=True)
class DatabaseDefinition:
    name: str
    owner: str
    encoding: str
    locale_provider: str
    lc_collate: str
    lc_ctype: str
    locale: str | None
    icu_rules: str | None
    tablespace: str
    connection_limit: int
    allow_connections: bool
    is_template: bool


@dataclass(frozen=True, order=True)
class ObjectGrant:
    object_type: str
    schema_name: str | None
    object_name: str
    column_name: str | None
    grantee: str
    privilege: str
    grantable: bool


@dataclass(frozen=True, order=True)
class DatabaseSetting:
    role_name: str | None
    name: str
    value: str


@dataclass(frozen=True, order=True)
class DefaultGrant:
    grantee: str
    privilege: str
    grantable: bool


@dataclass(frozen=True)
class DefaultPrivilegeSet:
    owner: str
    schema_name: str | None
    object_type: str
    grants: tuple[DefaultGrant, ...]


@dataclass(frozen=True)
class ResetSnapshot:
    database: DatabaseDefinition
    object_grants: tuple[ObjectGrant, ...]
    settings: tuple[DatabaseSetting, ...]
    default_privileges: tuple[DefaultPrivilegeSet, ...]


@dataclass(frozen=True)
class ResetTargetIdentity:
    database_name: str
    owner: str
    cluster_system_identifier: str


@dataclass(frozen=True)
class ResetRecoveryRecord:
    format: str
    version: int
    reset_id: str
    target: ResetTargetIdentity
    snapshot: ResetSnapshot
    state: str
    checksum: str


_PROVIDER_NAMES = {
    "b": "builtin",
    "c": "libc",
    "i": "icu",
}
_DEFAULT_OBJECT_TYPES = {
    "r": "TABLES",
    "S": "SEQUENCES",
    "f": "FUNCTIONS",
    "T": "TYPES",
    "n": "SCHEMAS",
}
_ALLOWED_PRIVILEGES = {
    "DATABASE": {"CONNECT", "CREATE", "TEMPORARY"},
    "SCHEMA": {"CREATE", "USAGE"},
    "TABLE": {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
        "MAINTAIN",
    },
    "SEQUENCE": {"USAGE", "SELECT", "UPDATE"},
    "COLUMN": {"SELECT", "INSERT", "UPDATE", "REFERENCES"},
}
_ALLOWED_DEFAULT_PRIVILEGES = {
    "TABLES": _ALLOWED_PRIVILEGES["TABLE"],
    "SEQUENCES": _ALLOWED_PRIVILEGES["SEQUENCE"],
    "FUNCTIONS": {"EXECUTE"},
    "TYPES": {"USAGE"},
    "SCHEMAS": _ALLOWED_PRIVILEGES["SCHEMA"],
}
_SETTING_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_SYSTEM_DATABASES = {"postgres", "template0", "template1"}
_RESET_RECOVERY_FORMAT = "dish-production-reset-recovery"
_RESET_RECOVERY_VERSION = 1
RESET_GUARD_SETTING = "dish.production_reset_incomplete"
_RESET_RECOVERY_STATES = {
    "snapshot_captured",
    "reset_started",
    "access_restored",
    "completed",
}
_RESET_STATE_TRANSITIONS = {
    "snapshot_captured": {"reset_started"},
    "reset_started": {"access_restored"},
    "access_restored": {"completed"},
    "completed": set(),
}


def maintenance_database_url(database_url: str) -> str:
    """Return the same server/user URL targeting the maintenance database."""
    return make_url(database_url).set(database="postgres").render_as_string(
        hide_password=False
    )


def redacted_database_url(database_url: str) -> str:
    """Return a log-safe SQLAlchemy URL that never includes the password."""
    return make_url(database_url).render_as_string(hide_password=True)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validated_reset_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProductionResetError(f"invalid production-reset id {value!r}") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ProductionResetError(
            f"production-reset id must use canonical UUID form, got {value!r}"
        )
    return canonical


def _snapshot_from_payload(value: object) -> ResetSnapshot:
    if not isinstance(value, dict):
        raise ProductionResetError("production-reset recovery snapshot is not an object")
    expected = {"database", "object_grants", "settings", "default_privileges"}
    if set(value) != expected:
        raise ProductionResetError("production-reset recovery snapshot has unexpected fields")
    try:
        database_raw = value["database"]
        grants_raw = value["object_grants"]
        settings_raw = value["settings"]
        defaults_raw = value["default_privileges"]
        if not isinstance(database_raw, dict):
            raise TypeError("database")
        if not isinstance(grants_raw, list):
            raise TypeError("object_grants")
        if not isinstance(settings_raw, list):
            raise TypeError("settings")
        if not isinstance(defaults_raw, list):
            raise TypeError("default_privileges")
        database = DatabaseDefinition(**database_raw)
        grants = tuple(ObjectGrant(**grant) for grant in grants_raw)
        settings = tuple(DatabaseSetting(**setting) for setting in settings_raw)
        default_privileges = tuple(
            DefaultPrivilegeSet(
                owner=default_set["owner"],
                schema_name=default_set["schema_name"],
                object_type=default_set["object_type"],
                grants=tuple(DefaultGrant(**grant) for grant in default_set["grants"]),
            )
            for default_set in defaults_raw
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionResetError(
            "production-reset recovery snapshot is malformed"
        ) from exc
    snapshot = ResetSnapshot(
        database=database,
        object_grants=grants,
        settings=settings,
        default_privileges=default_privileges,
    )
    if any(setting.name == RESET_GUARD_SETTING for setting in snapshot.settings):
        raise ProductionResetError(
            "production-reset recovery snapshot contains the reserved reset guard setting"
        )
    return snapshot


def _recovery_payload(
    *,
    reset_id: str,
    target: ResetTargetIdentity,
    snapshot: ResetSnapshot,
    state: str,
) -> dict[str, object]:
    if state not in _RESET_RECOVERY_STATES:
        raise ProductionResetError(f"invalid production-reset recovery state {state!r}")
    return {
        "format": _RESET_RECOVERY_FORMAT,
        "version": _RESET_RECOVERY_VERSION,
        "reset_id": _validated_reset_id(reset_id),
        "target": asdict(target),
        "snapshot": asdict(snapshot),
        "state": state,
    }


def _record_from_values(
    *,
    reset_id: str,
    target: ResetTargetIdentity,
    snapshot: ResetSnapshot,
    state: str,
) -> ResetRecoveryRecord:
    payload = _recovery_payload(
        reset_id=reset_id,
        target=target,
        snapshot=snapshot,
        state=state,
    )
    checksum = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return ResetRecoveryRecord(
        format=_RESET_RECOVERY_FORMAT,
        version=_RESET_RECOVERY_VERSION,
        reset_id=reset_id,
        target=target,
        snapshot=snapshot,
        state=state,
        checksum=checksum,
    )


def new_recovery_record(
    *,
    target: ResetTargetIdentity,
    snapshot: ResetSnapshot,
    reset_id: str | None = None,
) -> ResetRecoveryRecord:
    return _record_from_values(
        reset_id=reset_id or str(uuid.uuid4()),
        target=target,
        snapshot=snapshot,
        state="snapshot_captured",
    )


def _record_document(record: ResetRecoveryRecord) -> dict[str, object]:
    payload = _recovery_payload(
        reset_id=record.reset_id,
        target=record.target,
        snapshot=record.snapshot,
        state=record.state,
    )
    expected_checksum = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    if record.checksum != expected_checksum:
        raise ProductionResetError("production-reset recovery record checksum is inconsistent")
    return {**payload, "checksum": expected_checksum}


def _write_record_bytes(fd: int, document: dict[str, object]) -> None:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb", closefd=True) as handle:
        handle.write(_canonical_json_bytes(document))
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def create_recovery_record(path: Path, record: ResetRecoveryRecord) -> None:
    destination = Path(os.path.abspath(path.expanduser()))
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = _record_document(record)
    fd, raw_temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(raw_temporary)
    try:
        _write_record_bytes(fd, document)
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ProductionResetError(
                f"production-reset recovery record already exists and will not be reused: {destination}"
            ) from exc
        _fsync_directory(destination.parent)
    except OSError as exc:
        raise ProductionResetError(
            f"could not persist production-reset recovery record {destination}: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _replace_recovery_record(path: Path, record: ResetRecoveryRecord) -> None:
    destination = Path(os.path.abspath(path.expanduser()))
    document = _record_document(record)
    fd, raw_temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(raw_temporary)
    try:
        _write_record_bytes(fd, document)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except OSError as exc:
        raise ProductionResetError(
            f"could not update production-reset recovery record {destination}: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def load_recovery_record(path: Path) -> ResetRecoveryRecord:
    source = Path(os.path.abspath(path.expanduser()))
    if source.is_symlink():
        raise ProductionResetError(
            f"production-reset recovery record must not be a symlink: {source}"
        )
    try:
        stat_result = source.stat()
    except FileNotFoundError as exc:
        raise ProductionResetError(
            f"production-reset recovery record is missing: {source}"
        ) from exc
    except OSError as exc:
        raise ProductionResetError(
            f"could not inspect production-reset recovery record {source}: {exc}"
        ) from exc
    if not source.is_file():
        raise ProductionResetError(
            f"production-reset recovery record must be a regular non-symlink file: {source}"
        )
    if stat_result.st_mode & 0o077:
        raise ProductionResetError(
            f"production-reset recovery record permissions must not grant group/other access: {source}"
        )
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionResetError(
            f"could not read production-reset recovery record {source}: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ProductionResetError("production-reset recovery record is not an object")
    expected_fields = {
        "format",
        "version",
        "reset_id",
        "target",
        "snapshot",
        "state",
        "checksum",
    }
    if set(document) != expected_fields:
        raise ProductionResetError("production-reset recovery record has unexpected fields")
    if document["format"] != _RESET_RECOVERY_FORMAT:
        raise ProductionResetError("unsupported production-reset recovery record format")
    if document["version"] != _RESET_RECOVERY_VERSION:
        raise ProductionResetError(
            f"unsupported production-reset recovery record version {document['version']!r}"
        )
    reset_id = _validated_reset_id(str(document["reset_id"]))
    state = document["state"]
    if not isinstance(state, str) or state not in _RESET_RECOVERY_STATES:
        raise ProductionResetError("production-reset recovery record has invalid state")
    target_raw = document["target"]
    if not isinstance(target_raw, dict) or set(target_raw) != {
        "database_name",
        "owner",
        "cluster_system_identifier",
    }:
        raise ProductionResetError("production-reset recovery target identity is malformed")
    target = ResetTargetIdentity(
        database_name=str(target_raw["database_name"]),
        owner=str(target_raw["owner"]),
        cluster_system_identifier=str(target_raw["cluster_system_identifier"]),
    )
    snapshot = _snapshot_from_payload(document["snapshot"])
    payload = _recovery_payload(
        reset_id=reset_id,
        target=target,
        snapshot=snapshot,
        state=state,
    )
    checksum = document["checksum"]
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ProductionResetError("production-reset recovery record checksum is malformed")
    expected_checksum = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    if checksum != expected_checksum:
        raise ProductionResetError("production-reset recovery record checksum mismatch")
    if target.database_name != snapshot.database.name or target.owner != snapshot.database.owner:
        raise ProductionResetError(
            "production-reset recovery target identity does not match its original snapshot"
        )
    return ResetRecoveryRecord(
        format=_RESET_RECOVERY_FORMAT,
        version=_RESET_RECOVERY_VERSION,
        reset_id=reset_id,
        target=target,
        snapshot=snapshot,
        state=state,
        checksum=checksum,
    )


def transition_recovery_record(
    path: Path,
    *,
    expected_reset_id: str,
    expected_state: str,
    new_state: str,
) -> ResetRecoveryRecord:
    record = load_recovery_record(path)
    if record.reset_id != expected_reset_id:
        raise ProductionResetError(
            "production-reset recovery record reset_id changed during recovery"
        )
    if record.state != expected_state:
        raise ProductionResetError(
            "production-reset recovery record state changed unexpectedly: "
            f"expected {expected_state!r}, found {record.state!r}"
        )
    if new_state not in _RESET_STATE_TRANSITIONS[record.state]:
        raise ProductionResetError(
            f"illegal production-reset recovery transition {record.state!r} -> {new_state!r}"
        )
    updated = _record_from_values(
        reset_id=record.reset_id,
        target=record.target,
        snapshot=record.snapshot,
        state=new_state,
    )
    _replace_recovery_record(path, updated)
    verified = load_recovery_record(path)
    if verified != updated:
        raise ProductionResetError("production-reset recovery record write verification failed")
    return verified


def validate_cli_target(
    *,
    database_url: str,
    expected_database_name: str,
    confirmed_database_name: str,
    capture_environment: str,
) -> None:
    if capture_environment != "production":
        raise ProductionResetError(
            "production reset requires DISH_PG_CAPTURE_ENVIRONMENT=production"
        )
    actual_database_name = make_url(database_url).database
    if actual_database_name != expected_database_name:
        raise ProductionResetError(
            "DISH_PG_DATABASE_URL database does not match "
            "DISH_PG_EXPECTED_DATABASE_NAME"
        )
    if confirmed_database_name != expected_database_name:
        raise ProductionResetError(
            "--confirm-database-name must exactly match "
            "DISH_PG_EXPECTED_DATABASE_NAME"
        )
    if expected_database_name in _SYSTEM_DATABASES:
        raise ProductionResetError(
            f"refusing to reset PostgreSQL system database {expected_database_name!r}"
        )
    if expected_database_name.endswith("_test"):
        raise ProductionResetError(
            "production reset refuses a database name ending in '_test'"
        )


def _quote_identifier(connection: Connection, value: str) -> str:
    return connection.dialect.identifier_preparer.quote_identifier(value)


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _role_sql(connection: Connection, role_name: str) -> str:
    if role_name == "PUBLIC":
        return "PUBLIC"
    return _quote_identifier(connection, role_name)


def _load_database_definition(
    connection: Connection, expected_database_name: str
) -> DatabaseDefinition:
    row = connection.execute(
        text(
            """
            SELECT
                d.datname,
                owner.rolname AS owner,
                pg_encoding_to_char(d.encoding) AS encoding,
                d.datlocprovider,
                d.datcollate,
                d.datctype,
                d.datlocale,
                d.daticurules,
                tablespace.spcname AS tablespace,
                d.datconnlimit,
                d.datallowconn,
                d.datistemplate
            FROM pg_database AS d
            JOIN pg_roles AS owner ON owner.oid = d.datdba
            JOIN pg_tablespace AS tablespace ON tablespace.oid = d.dattablespace
            WHERE d.datname = :database_name
            """
        ),
        {"database_name": expected_database_name},
    ).mappings().one_or_none()
    if row is None:
        raise ProductionResetError(
            f"target database {expected_database_name!r} does not exist"
        )
    try:
        provider = _PROVIDER_NAMES[str(row["datlocprovider"])]
    except KeyError as exc:
        raise ProductionResetError(
            f"unsupported PostgreSQL locale provider {row['datlocprovider']!r}"
        ) from exc
    return DatabaseDefinition(
        name=str(row["datname"]),
        owner=str(row["owner"]),
        encoding=str(row["encoding"]),
        locale_provider=provider,
        lc_collate=str(row["datcollate"]),
        lc_ctype=str(row["datctype"]),
        locale=None if row["datlocale"] is None else str(row["datlocale"]),
        icu_rules=None if row["daticurules"] is None else str(row["daticurules"]),
        tablespace=str(row["tablespace"]),
        connection_limit=int(row["datconnlimit"]),
        allow_connections=bool(row["datallowconn"]),
        is_template=bool(row["datistemplate"]),
    )


def _database_exists(connection: Connection, database_name: str) -> bool:
    return bool(
        connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :database_name)"),
            {"database_name": database_name},
        ).scalar_one()
    )


def _maintenance_actor_identity(connection: Connection) -> tuple[str, bool]:
    row = connection.execute(
        text(
            "SELECT current_user AS role_name, role.rolsuper AS is_superuser "
            "FROM pg_roles AS role WHERE role.rolname = current_user"
        )
    ).mappings().one()
    return str(row["role_name"]), bool(row["is_superuser"])


def _cluster_system_identifier(connection: Connection) -> str:
    return str(
        connection.execute(
            text("SELECT system_identifier::text FROM pg_control_system()")
        ).scalar_one()
    )


def _load_reset_guard(connection: Connection, database_name: str) -> str | None:
    rows = connection.execute(
        text(
            """
            SELECT setting_role.rolname AS role_name, setting
            FROM pg_db_role_setting AS role_setting
            LEFT JOIN pg_roles AS setting_role ON setting_role.oid = role_setting.setrole
            CROSS JOIN LATERAL unnest(role_setting.setconfig) AS setting
            WHERE role_setting.setdatabase = (
                SELECT oid FROM pg_database WHERE datname = :database_name
            )
            ORDER BY setting_role.rolname NULLS FIRST, setting
            """
        ),
        {"database_name": database_name},
    ).mappings()
    values: list[str] = []
    for row in rows:
        raw = str(row["setting"])
        name, separator, value = raw.partition("=")
        if not separator or name != RESET_GUARD_SETTING:
            continue
        if row["role_name"] is not None:
            raise ProductionResetError(
                "reserved production-reset guard is contaminated by a role-scoped setting"
            )
        values.append(value)
    if not values:
        return None
    if len(values) != 1:
        raise ProductionResetError(
            "reserved production-reset guard has multiple database-scoped values"
        )
    try:
        return _validated_reset_id(values[0])
    except ProductionResetError as exc:
        raise ProductionResetError(
            "reserved production-reset guard contains a non-canonical reset id"
        ) from exc


def read_reset_guard(database_url: str, expected_database_name: str) -> str | None:
    engine = create_engine(maintenance_database_url(database_url))
    try:
        with engine.connect() as connection:
            return _load_reset_guard(connection, expected_database_name)
    finally:
        engine.dispose()


def capture_reset_target_identity(
    database_url: str, snapshot: ResetSnapshot
) -> ResetTargetIdentity:
    engine = create_engine(maintenance_database_url(database_url))
    try:
        with engine.connect() as connection:
            current = _load_database_definition(connection, snapshot.database.name)
            if current != snapshot.database:
                raise ProductionResetError(
                    "database identity changed before reset recovery lineage was captured"
                )
            role_name, is_superuser = _maintenance_actor_identity(connection)
            if role_name != snapshot.database.owner or not is_superuser:
                raise ProductionResetError(
                    "maintenance connection is not the verified superuser database owner"
                )
            if _load_reset_guard(connection, snapshot.database.name) is not None:
                raise ProductionResetError(
                    "reserved production-reset guard became active before recovery lineage capture"
                )
            return ResetTargetIdentity(
                database_name=snapshot.database.name,
                owner=snapshot.database.owner,
                cluster_system_identifier=_cluster_system_identifier(connection),
            )
    finally:
        engine.dispose()


def validate_recovery_record_target(
    database_url: str,
    expected_database_name: str,
    record: ResetRecoveryRecord,
) -> None:
    if expected_database_name != record.target.database_name:
        raise ProductionResetError(
            "production-reset recovery record targets a different database name"
        )
    engine = create_engine(maintenance_database_url(database_url))
    try:
        with engine.connect() as connection:
            role_name, is_superuser = _maintenance_actor_identity(connection)
            if role_name != record.target.owner or not is_superuser:
                raise ProductionResetError(
                    "maintenance connection does not match recovery-record owner/superuser identity"
                )
            cluster_id = _cluster_system_identifier(connection)
            if cluster_id != record.target.cluster_system_identifier:
                raise ProductionResetError(
                    "production-reset recovery record cluster identity mismatch"
                )
            if _database_exists(connection, expected_database_name):
                current = _load_database_definition(connection, expected_database_name)
                if current.owner != record.target.owner:
                    raise ProductionResetError(
                        "production-reset recovery record database owner mismatch"
                    )
    finally:
        engine.dispose()


def clear_reset_guard(
    database_url: str, expected_database_name: str, expected_reset_id: str
) -> None:
    expected_reset_id = _validated_reset_id(expected_reset_id)
    engine = create_engine(maintenance_database_url(database_url))
    try:
        with engine.connect() as raw_connection:
            connection = raw_connection.execution_options(isolation_level="AUTOCOMMIT")
            if not _database_exists(connection, expected_database_name):
                raise ProductionResetError(
                    "cannot clear production-reset guard because target database is missing"
                )
            current = _load_database_definition(connection, expected_database_name)
            role_name, is_superuser = _maintenance_actor_identity(connection)
            if role_name != current.owner or not is_superuser:
                raise ProductionResetError(
                    "maintenance connection lost verified owner/superuser identity"
                )
            actual = _load_reset_guard(connection, expected_database_name)
            if actual != expected_reset_id:
                raise ProductionResetError(
                    "production-reset guard/reset-id mismatch; refusing to clear guard"
                )
            database_sql = _quote_identifier(connection, expected_database_name)
            connection.exec_driver_sql(
                f"ALTER DATABASE {database_sql} RESET {RESET_GUARD_SETTING}"
            )
            if _load_reset_guard(connection, expected_database_name) is not None:
                raise ProductionResetError("production-reset guard clear verification failed")
    finally:
        engine.dispose()


def _validate_connected_identity(
    connection: Connection, database: DatabaseDefinition
) -> None:
    identity = connection.execute(
        text(
            """
            SELECT current_database() AS database_name,
                   current_user AS role_name,
                   role.rolsuper AS is_superuser
            FROM pg_roles AS role
            WHERE role.rolname = current_user
            """
        )
    ).mappings().one()
    if str(identity["database_name"]) != database.name:
        raise ProductionResetError("connected database changed during reset preflight")
    if str(identity["role_name"]) != database.owner:
        raise ProductionResetError(
            "DISH_PG_DATABASE_URL must connect as the current database owner"
        )
    if not bool(identity["is_superuser"]):
        raise ProductionResetError(
            "production reset requires the database owner role to be PostgreSQL "
            "superuser so foreign blocking sessions can be terminated safely"
        )
    if database.is_template:
        raise ProductionResetError("refusing to reset a template database")
    if not database.allow_connections:
        raise ProductionResetError(
            "target database already has ALLOW_CONNECTIONS=false; investigate before reset"
        )


def _load_object_grants(
    connection: Connection, database: DatabaseDefinition
) -> tuple[ObjectGrant, ...]:
    rows = connection.execute(
        text(
            """
            WITH database_grants AS (
                SELECT
                    'DATABASE'::text AS object_type,
                    NULL::text AS schema_name,
                    d.datname::text AS object_name,
                    NULL::text AS column_name,
                    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE grantee.rolname END::text AS grantee,
                    acl.privilege_type::text AS privilege,
                    acl.is_grantable AS grantable
                FROM pg_database AS d
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(d.datacl, acldefault('d', d.datdba))
                ) AS acl
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE d.datname = :database_name
                  AND acl.grantee <> d.datdba
            ),
            schema_grants AS (
                SELECT
                    'SCHEMA'::text AS object_type,
                    n.nspname::text AS schema_name,
                    n.nspname::text AS object_name,
                    NULL::text AS column_name,
                    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE grantee.rolname END::text AS grantee,
                    acl.privilege_type::text AS privilege,
                    acl.is_grantable AS grantable
                FROM pg_namespace AS n
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(n.nspacl, acldefault('n', n.nspowner))
                ) AS acl
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE n.nspname <> 'information_schema'
                  AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                  AND acl.grantee <> n.nspowner
            ),
            relation_grants AS (
                SELECT
                    CASE WHEN c.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END::text AS object_type,
                    n.nspname::text AS schema_name,
                    c.relname::text AS object_name,
                    NULL::text AS column_name,
                    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE grantee.rolname END::text AS grantee,
                    acl.privilege_type::text AS privilege,
                    acl.is_grantable AS grantable
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                CROSS JOIN LATERAL aclexplode(c.relacl) AS acl
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE c.relacl IS NOT NULL
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
                  AND n.nspname <> 'information_schema'
                  AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                  AND acl.grantee <> c.relowner
            ),
            column_grants AS (
                SELECT
                    'COLUMN'::text AS object_type,
                    n.nspname::text AS schema_name,
                    c.relname::text AS object_name,
                    a.attname::text AS column_name,
                    CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE grantee.rolname END::text AS grantee,
                    acl.privilege_type::text AS privilege,
                    acl.is_grantable AS grantable
                FROM pg_attribute AS a
                JOIN pg_class AS c ON c.oid = a.attrelid
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                CROSS JOIN LATERAL aclexplode(a.attacl) AS acl
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE a.attacl IS NOT NULL
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND n.nspname <> 'information_schema'
                  AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                  AND acl.grantee <> c.relowner
            )
            SELECT * FROM database_grants
            UNION ALL SELECT * FROM schema_grants
            UNION ALL SELECT * FROM relation_grants
            UNION ALL SELECT * FROM column_grants
            ORDER BY object_type, schema_name, object_name, column_name, grantee, privilege, grantable
            """
        ),
        {"database_name": database.name},
    ).mappings()
    grants: list[ObjectGrant] = []
    for row in rows:
        object_type = str(row["object_type"])
        privilege = str(row["privilege"]).upper()
        if privilege not in _ALLOWED_PRIVILEGES[object_type]:
            raise ProductionResetError(
                f"unsupported {object_type} privilege {privilege!r} in reset snapshot"
            )
        grantee = str(row["grantee"])
        if not grantee:
            raise ProductionResetError("reset snapshot contains an unresolved grantee role")
        grants.append(
            ObjectGrant(
                object_type=object_type,
                schema_name=None
                if row["schema_name"] is None
                else str(row["schema_name"]),
                object_name=str(row["object_name"]),
                column_name=None
                if row["column_name"] is None
                else str(row["column_name"]),
                grantee=grantee,
                privilege=privilege,
                grantable=bool(row["grantable"]),
            )
        )
    return tuple(grants)


def _load_database_settings(
    connection: Connection,
    database: DatabaseDefinition,
    *,
    allow_reset_guard: bool = False,
) -> tuple[DatabaseSetting, ...]:
    rows = connection.execute(
        text(
            """
            SELECT setting_role.rolname AS role_name, setting
            FROM pg_db_role_setting AS role_setting
            LEFT JOIN pg_roles AS setting_role ON setting_role.oid = role_setting.setrole
            CROSS JOIN LATERAL unnest(role_setting.setconfig) AS setting
            WHERE role_setting.setdatabase = (
                SELECT oid FROM pg_database WHERE datname = :database_name
            )
            ORDER BY setting_role.rolname NULLS FIRST, setting
            """
        ),
        {"database_name": database.name},
    ).mappings()
    settings: list[DatabaseSetting] = []
    for row in rows:
        raw = str(row["setting"])
        name, separator, value = raw.partition("=")
        if not separator or not _SETTING_NAME.fullmatch(name):
            raise ProductionResetError(
                f"cannot safely restore database setting {raw!r}"
            )
        if name == RESET_GUARD_SETTING:
            if allow_reset_guard:
                continue
            raise ProductionResetError(
                "reserved production-reset guard setting already exists on the target"
            )
        settings.append(
            DatabaseSetting(
                role_name=None
                if row["role_name"] is None
                else str(row["role_name"]),
                name=name,
                value=value,
            )
        )
    return tuple(settings)


def _load_default_privileges(connection: Connection) -> tuple[DefaultPrivilegeSet, ...]:
    row_metadata = connection.execute(
        text(
            """
            SELECT defaults.oid,
                   owner.rolname AS owner,
                   namespace.nspname AS schema_name,
                   defaults.defaclobjtype AS object_type
            FROM pg_default_acl AS defaults
            JOIN pg_roles AS owner ON owner.oid = defaults.defaclrole
            LEFT JOIN pg_namespace AS namespace ON namespace.oid = defaults.defaclnamespace
            ORDER BY owner.rolname, namespace.nspname NULLS FIRST, defaults.defaclobjtype
            """
        )
    ).mappings().all()
    sets: list[DefaultPrivilegeSet] = []
    for metadata in row_metadata:
        object_type_code = str(metadata["object_type"])
        try:
            object_type = _DEFAULT_OBJECT_TYPES[object_type_code]
        except KeyError as exc:
            raise ProductionResetError(
                f"unsupported pg_default_acl object type {object_type_code!r}"
            ) from exc
        grant_rows = connection.execute(
            text(
                """
                SELECT CASE WHEN acl.grantee = 0 THEN 'PUBLIC' ELSE grantee.rolname END AS grantee,
                       acl.privilege_type AS privilege,
                       acl.is_grantable AS grantable,
                       acl.grantee AS grantee_oid,
                       defaults.defaclrole AS owner_oid
                FROM pg_default_acl AS defaults
                CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS acl
                LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                WHERE defaults.oid = :oid
                ORDER BY grantee, privilege, grantable
                """
            ),
            {"oid": int(metadata["oid"])},
        ).mappings()
        grants: list[DefaultGrant] = []
        for row in grant_rows:
            if int(row["grantee_oid"]) == int(row["owner_oid"]):
                continue
            privilege = str(row["privilege"]).upper()
            if privilege not in _ALLOWED_DEFAULT_PRIVILEGES[object_type]:
                raise ProductionResetError(
                    f"unsupported default {object_type} privilege {privilege!r}"
                )
            grantee = str(row["grantee"])
            if not grantee:
                raise ProductionResetError(
                    "default privilege snapshot contains an unresolved grantee role"
                )
            grants.append(
                DefaultGrant(
                    grantee=grantee,
                    privilege=privilege,
                    grantable=bool(row["grantable"]),
                )
            )
        sets.append(
            DefaultPrivilegeSet(
                owner=str(metadata["owner"]),
                schema_name=None
                if metadata["schema_name"] is None
                else str(metadata["schema_name"]),
                object_type=object_type,
                grants=tuple(grants),
            )
        )
    return tuple(sets)


def _check_non_session_drop_blockers(connection: Connection, database_name: str) -> None:
    prepared_count = int(
        connection.execute(
            text("SELECT count(*) FROM pg_prepared_xacts WHERE database = :database_name"),
            {"database_name": database_name},
        ).scalar_one()
    )
    slot_count = int(
        connection.execute(
            text(
                "SELECT count(*) FROM pg_replication_slots "
                "WHERE database = :database_name"
            ),
            {"database_name": database_name},
        ).scalar_one()
    )
    if prepared_count or slot_count:
        raise ProductionResetError(
            "target has drop blockers that this script must not remove automatically: "
            f"prepared_transactions={prepared_count}, logical_replication_slots={slot_count}"
        )


def snapshot_database_state(
    database_url: str, expected_database_name: str
) -> ResetSnapshot:
    """Read and validate every state item that must survive a full database drop."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            database = _load_database_definition(connection, expected_database_name)
            _validate_connected_identity(connection, database)
            subscription_count = int(
                connection.execute(text("SELECT count(*) FROM pg_subscription")).scalar_one()
            )
            if subscription_count:
                raise ProductionResetError(
                    "target has PostgreSQL subscriptions; reset will not remove them automatically"
                )
            object_grants = _load_object_grants(connection, database)
            settings = _load_database_settings(connection, database)
            default_privileges = _load_default_privileges(connection)
    finally:
        engine.dispose()

    maintenance_engine = create_engine(maintenance_database_url(database_url))
    try:
        with maintenance_engine.connect() as connection:
            current = _load_database_definition(connection, expected_database_name)
            if current != database:
                raise ProductionResetError(
                    "database identity changed between target and maintenance preflight"
                )
            identity = connection.execute(
                text(
                    "SELECT current_user AS role_name, role.rolsuper AS is_superuser "
                    "FROM pg_roles AS role WHERE role.rolname = current_user"
                )
            ).mappings().one()
            if str(identity["role_name"]) != database.owner or not bool(
                identity["is_superuser"]
            ):
                raise ProductionResetError(
                    "maintenance connection is not the verified superuser database owner"
                )
            _check_non_session_drop_blockers(connection, database.name)
    finally:
        maintenance_engine.dispose()

    return ResetSnapshot(
        database=database,
        object_grants=object_grants,
        settings=settings,
        default_privileges=default_privileges,
    )


def _database_create_sql(
    connection: Connection,
    database: DatabaseDefinition,
    *,
    allow_connections: bool | None = None,
) -> str:
    name = _quote_identifier(connection, database.name)
    owner = _quote_identifier(connection, database.owner)
    tablespace = _quote_identifier(connection, database.tablespace)
    parts = [
        f"CREATE DATABASE {name} WITH",
        f"OWNER = {owner}",
        "TEMPLATE = template0",
        f"ENCODING = {_quote_literal(database.encoding)}",
        f"LOCALE_PROVIDER = {database.locale_provider}",
    ]
    if database.locale_provider == "libc":
        parts.extend(
            [
                f"LC_COLLATE = {_quote_literal(database.lc_collate)}",
                f"LC_CTYPE = {_quote_literal(database.lc_ctype)}",
            ]
        )
    elif database.locale_provider == "icu":
        if not database.locale:
            raise ProductionResetError("ICU database is missing its catalog locale")
        parts.extend(
            [
                f"ICU_LOCALE = {_quote_literal(database.locale)}",
                f"LC_COLLATE = {_quote_literal(database.lc_collate)}",
                f"LC_CTYPE = {_quote_literal(database.lc_ctype)}",
            ]
        )
        if database.icu_rules:
            parts.append(f"ICU_RULES = {_quote_literal(database.icu_rules)}")
    elif database.locale_provider == "builtin":
        if not database.locale:
            raise ProductionResetError("builtin database is missing its catalog locale")
        parts.append(f"BUILTIN_LOCALE = {_quote_literal(database.locale)}")
    effective_allow_connections = (
        database.allow_connections if allow_connections is None else allow_connections
    )
    parts.extend(
        [
            f"TABLESPACE = {tablespace}",
            "ALLOW_CONNECTIONS = " + ("true" if effective_allow_connections else "false"),
            f"CONNECTION LIMIT = {database.connection_limit}",
        ]
    )
    return " ".join(parts)


def _active_sessions(connection: Connection, database_name: str) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT pid, usename, application_name, client_addr::text AS client_addr, state
                FROM pg_stat_activity
                WHERE datname = :database_name
                  AND pid <> pg_backend_pid()
                ORDER BY pid
                """
            ),
            {"database_name": database_name},
        ).mappings()
    ]


def recreate_database(
    database_url: str,
    snapshot: ResetSnapshot,
    *,
    reset_id: str,
    resume: bool = False,
    log: Callable[[str], None] | None = None,
    wait_seconds: float = 5.0,
) -> None:
    """Fence and re-create the target while preserving one durable reset lineage."""
    emit = log or (lambda _message: None)
    reset_id = _validated_reset_id(reset_id)
    database = snapshot.database
    maintenance_engine = create_engine(maintenance_database_url(database_url))
    changed_allow_connections = False
    dropped = False
    try:
        with maintenance_engine.connect() as raw_connection:
            connection = raw_connection.execution_options(isolation_level="AUTOCOMMIT")
            current = (
                _load_database_definition(connection, database.name)
                if _database_exists(connection, database.name)
                else None
            )
            role_name, is_superuser = _maintenance_actor_identity(connection)
            if role_name != database.owner or not is_superuser:
                raise ProductionResetError(
                    "maintenance connection lost verified owner/superuser identity"
                )

            current_guard = _load_reset_guard(connection, database.name)
            if resume:
                if current_guard is not None and current_guard != reset_id:
                    raise ProductionResetError(
                        "production-reset guard/reset-id mismatch; refusing to mutate target"
                    )
                if current is not None and replace(
                    current, allow_connections=database.allow_connections
                ) != database:
                    raise ProductionResetError(
                        "database identity/configuration changed from the retained original snapshot"
                    )
            else:
                if current_guard is not None:
                    raise ProductionResetError(
                        "active production-reset guard exists; ordinary reset cannot establish a new baseline"
                    )
                if current is None or current != database:
                    raise ProductionResetError(
                        "database identity/configuration changed since reset snapshot"
                    )

            if current is not None:
                _check_non_session_drop_blockers(connection, database.name)
                initial_sessions = _active_sessions(connection, database.name)
                if initial_sessions:
                    identities = ", ".join(
                        f"pid={row['pid']} role={row['usename']} app={row['application_name'] or '-'}"
                        for row in initial_sessions
                    )
                    emit(
                        f"found {len(initial_sessions)} blocking connection(s) before reset: "
                        f"{identities}"
                    )
                else:
                    emit("no blocking connections present before reset")

                database_sql = _quote_identifier(connection, database.name)
                if current.allow_connections:
                    connection.exec_driver_sql(
                        f"ALTER DATABASE {database_sql} WITH ALLOW_CONNECTIONS false"
                    )
                    changed_allow_connections = True

                sessions = _active_sessions(connection, database.name)
                for session in sessions:
                    terminated = bool(
                        connection.execute(
                            text("SELECT pg_terminate_backend(:pid)"),
                            {"pid": int(session["pid"])},
                        ).scalar_one()
                    )
                    if not terminated:
                        raise ProductionResetError(
                            f"PostgreSQL refused to terminate blocking pid {session['pid']}"
                        )

                deadline = time.monotonic() + wait_seconds
                remaining = _active_sessions(connection, database.name)
                while remaining and time.monotonic() < deadline:
                    time.sleep(0.05)
                    remaining = _active_sessions(connection, database.name)
                if remaining:
                    pids = ", ".join(str(row["pid"]) for row in remaining)
                    raise ProductionResetError(
                        f"blocking connections remained after termination request: {pids}"
                    )
                emit("connection fence active; all blocking sessions are gone")
                connection.exec_driver_sql(f"DROP DATABASE {database_sql}")
                dropped = True
            else:
                database_sql = _quote_identifier(connection, database.name)
                emit("target database is absent; resuming from retained reset lineage")

            create_sql = _database_create_sql(
                connection, database, allow_connections=False
            )
            connection.exec_driver_sql(create_sql)
            emit(
                "database recreated with ALLOW_CONNECTIONS=false; installing durable reset guard"
            )

            connection.exec_driver_sql(
                f"ALTER DATABASE {database_sql} SET {RESET_GUARD_SETTING} "
                f"TO {_quote_literal(reset_id)}"
            )
            installed_guard = _load_reset_guard(connection, database.name)
            if installed_guard != reset_id:
                raise ProductionResetError(
                    "production-reset guard installation verification failed"
                )
            connection.exec_driver_sql(
                f"REVOKE ALL PRIVILEGES ON DATABASE {database_sql} FROM PUBLIC"
            )
            connection.exec_driver_sql(
                f"ALTER DATABASE {database_sql} WITH ALLOW_CONNECTIONS true"
            )
            emit(
                "database recreated; reset guard active and non-owner access remains fenced pending prepare"
            )
    except Exception:
        if changed_allow_connections and not dropped:
            try:
                with maintenance_engine.connect() as raw_connection:
                    connection = raw_connection.execution_options(
                        isolation_level="AUTOCOMMIT"
                    )
                    if _database_exists(connection, database.name):
                        database_sql = _quote_identifier(connection, database.name)
                        connection.exec_driver_sql(
                            f"ALTER DATABASE {database_sql} WITH ALLOW_CONNECTIONS true"
                        )
            except Exception:
                pass
        raise
    finally:
        maintenance_engine.dispose()


def _grant_statement(connection: Connection, grant: ObjectGrant) -> str:
    allowed = _ALLOWED_PRIVILEGES.get(grant.object_type)
    if allowed is None or grant.privilege not in allowed:
        raise ProductionResetError(
            f"cannot restore unsupported {grant.object_type} privilege {grant.privilege!r}"
        )
    role = _role_sql(connection, grant.grantee)
    suffix = " WITH GRANT OPTION" if grant.grantable else ""
    privilege = grant.privilege
    if grant.object_type == "DATABASE":
        target = _quote_identifier(connection, grant.object_name)
        return f"GRANT {privilege} ON DATABASE {target} TO {role}{suffix}"
    if grant.object_type == "SCHEMA":
        target = _quote_identifier(connection, grant.object_name)
        return f"GRANT {privilege} ON SCHEMA {target} TO {role}{suffix}"
    if grant.schema_name is None:
        raise ProductionResetError(f"{grant.object_type} grant is missing its schema")
    target = (
        f"{_quote_identifier(connection, grant.schema_name)}."
        f"{_quote_identifier(connection, grant.object_name)}"
    )
    if grant.object_type == "TABLE":
        return f"GRANT {privilege} ON TABLE {target} TO {role}{suffix}"
    if grant.object_type == "SEQUENCE":
        return f"GRANT {privilege} ON SEQUENCE {target} TO {role}{suffix}"
    if grant.object_type == "COLUMN":
        if grant.column_name is None:
            raise ProductionResetError("column grant is missing its column name")
        column = _quote_identifier(connection, grant.column_name)
        return f"GRANT {privilege} ({column}) ON TABLE {target} TO {role}{suffix}"
    raise ProductionResetError(f"unsupported grant object type {grant.object_type!r}")


def _group_default_grants(
    grants: Iterable[DefaultGrant],
) -> dict[tuple[str, bool], list[str]]:
    grouped: dict[tuple[str, bool], list[str]] = {}
    for grant in grants:
        grouped.setdefault((grant.grantee, grant.grantable), []).append(grant.privilege)
    return grouped


def _restore_default_privileges(
    connection: Connection, default_set: DefaultPrivilegeSet
) -> None:
    if default_set.object_type not in _ALLOWED_DEFAULT_PRIVILEGES:
        raise ProductionResetError(
            f"unsupported default privilege object type {default_set.object_type!r}"
        )
    owner = _quote_identifier(connection, default_set.owner)
    schema_clause = ""
    if default_set.schema_name is not None:
        if default_set.object_type == "SCHEMAS":
            raise ProductionResetError(
                "PostgreSQL does not allow IN SCHEMA for default schema privileges"
            )
        schema_clause = (
            " IN SCHEMA " + _quote_identifier(connection, default_set.schema_name)
        )
    prefix = f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner}{schema_clause}"

    grantees = {grant.grantee for grant in default_set.grants}
    grantees.add("PUBLIC")
    for grantee in sorted(grantees):
        connection.exec_driver_sql(
            f"{prefix} REVOKE ALL PRIVILEGES ON {default_set.object_type} "
            f"FROM {_role_sql(connection, grantee)}"
        )
    for (grantee, grantable), privileges in sorted(
        _group_default_grants(default_set.grants).items()
    ):
        invalid = set(privileges) - _ALLOWED_DEFAULT_PRIVILEGES[default_set.object_type]
        if invalid:
            raise ProductionResetError(
                f"unsupported default privileges: {sorted(invalid)!r}"
            )
        suffix = " WITH GRANT OPTION" if grantable else ""
        connection.exec_driver_sql(
            f"{prefix} GRANT {', '.join(sorted(privileges))} "
            f"ON {default_set.object_type} TO {_role_sql(connection, grantee)}{suffix}"
        )


def _restore_settings(connection: Connection, snapshot: ResetSnapshot) -> None:
    database_sql = _quote_identifier(connection, snapshot.database.name)
    for setting in snapshot.settings:
        if not _SETTING_NAME.fullmatch(setting.name):
            raise ProductionResetError(
                f"unsafe database setting name {setting.name!r} in reset snapshot"
            )
        if setting.name == RESET_GUARD_SETTING:
            raise ProductionResetError(
                "reset snapshot attempts to restore the reserved production-reset guard"
            )
        value = _quote_literal(setting.value)
        if setting.role_name is None:
            connection.exec_driver_sql(
                f"ALTER DATABASE {database_sql} SET {setting.name} TO {value}"
            )
        else:
            role = _quote_identifier(connection, setting.role_name)
            connection.exec_driver_sql(
                f"ALTER ROLE {role} IN DATABASE {database_sql} "
                f"SET {setting.name} TO {value}"
            )


def _assert_roles_exist(connection: Connection, snapshot: ResetSnapshot) -> None:
    roles = {
        grant.grantee
        for grant in snapshot.object_grants
        if grant.grantee != "PUBLIC"
    }
    roles.update(
        grant.grantee
        for default_set in snapshot.default_privileges
        for grant in default_set.grants
        if grant.grantee != "PUBLIC"
    )
    roles.update(
        setting.role_name for setting in snapshot.settings if setting.role_name is not None
    )
    roles.update(default_set.owner for default_set in snapshot.default_privileges)
    if not roles:
        return
    existing = {
        str(value)
        for value in connection.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
            {"roles": sorted(roles)},
        ).scalars()
    }
    missing = sorted(roles - existing)
    if missing:
        raise ProductionResetError(
            "cannot restore grants/settings because PostgreSQL role(s) are missing: "
            + ", ".join(missing)
        )


def _verify_restored_state(connection: Connection, snapshot: ResetSnapshot) -> None:
    current_database = _load_database_definition(connection, snapshot.database.name)
    if current_database != snapshot.database:
        raise ProductionResetError(
            "recreated database identity/configuration does not match the pre-reset snapshot"
        )
    current_grants = set(_load_object_grants(connection, snapshot.database))
    missing_grants = sorted(set(snapshot.object_grants) - current_grants)
    if missing_grants:
        raise ProductionResetError(
            f"grant verification failed; {len(missing_grants)} pre-reset grant(s) are missing"
        )
    current_settings = set(
        _load_database_settings(
            connection, snapshot.database, allow_reset_guard=True
        )
    )
    missing_settings = sorted(set(snapshot.settings) - current_settings)
    if missing_settings:
        raise ProductionResetError(
            f"setting verification failed; {len(missing_settings)} pre-reset setting(s) are missing"
        )
    current_defaults = set(_load_default_privileges(connection))
    missing_defaults = sorted(
        set(snapshot.default_privileges) - current_defaults,
        key=lambda value: (value.owner, value.schema_name or "", value.object_type),
    )
    if missing_defaults:
        raise ProductionResetError(
            "default-privilege verification failed; "
            f"{len(missing_defaults)} pre-reset definition(s) are missing"
        )


def verify_database_access(database_url: str, snapshot: ResetSnapshot) -> None:
    """Verify the retained original access snapshot without changing PostgreSQL state."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            _assert_roles_exist(connection, snapshot)
            _verify_restored_state(connection, snapshot)
    finally:
        engine.dispose()


def restore_database_access(
    database_url: str,
    snapshot: ResetSnapshot,
    *,
    reset_id: str,
) -> None:
    """Restore the retained original grants/settings under the matching active guard."""
    reset_id = _validated_reset_id(reset_id)
    actual_guard = read_reset_guard(database_url, snapshot.database.name)
    if actual_guard != reset_id:
        raise ProductionResetError(
            "production-reset guard/reset-id mismatch; refusing to restore access"
        )
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            _assert_roles_exist(connection, snapshot)

            database_sql = _quote_identifier(connection, snapshot.database.name)
            connection.exec_driver_sql(
                f"REVOKE ALL PRIVILEGES ON DATABASE {database_sql} FROM PUBLIC"
            )
            schema_names = sorted(
                {
                    grant.object_name
                    for grant in snapshot.object_grants
                    if grant.object_type == "SCHEMA"
                }
            )
            for schema_name in schema_names:
                schema_sql = _quote_identifier(connection, schema_name)
                connection.exec_driver_sql(
                    f"REVOKE ALL PRIVILEGES ON SCHEMA {schema_sql} FROM PUBLIC"
                )

            for default_set in snapshot.default_privileges:
                _restore_default_privileges(connection, default_set)
            for grant in snapshot.object_grants:
                connection.exec_driver_sql(_grant_statement(connection, grant))
            _restore_settings(connection, snapshot)
            _verify_restored_state(connection, snapshot)
    finally:
        engine.dispose()
