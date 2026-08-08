"""Current Honest protocol/schema compatibility resolution."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .constants import (
    DISH_VERSION_FILENAME,
    HONEST_PATH_ENV,
    PROTOCOL_FILENAMES,
    SUPPORTED_PROTOCOL_VERSION,
    SUPPORTED_TASK_SCHEMA_VERSION,
    TASK_SCHEMA_FILENAME,
)
from .errors import ReleaseResolutionError
from .models import ReadOnlyLegacyAdapter, ResolvedRelease, VerificationProtocolSnapshot
from .schema_validation import validate_task_schema_shape

_VERSION_KEYS = ("PROTOCOL_VERSION", "SCHEMA_VERSION")
_GIT_RELEASE_RE = re.compile(r"^(?:git:)?(?P<commit>[0-9a-f]{7,64})$")
_HASH_RELEASE_RE = re.compile(
    r"^sha256:(?P<digest>[0-9a-f]{64}); "
    r"read-at=(?P<read_at>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)$"
)


def configured_honest_path(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the explicitly configured rollout checkout or fail closed."""

    env = os.environ if environ is None else environ
    raw = str(env.get(HONEST_PATH_ENV, "")).strip()
    if not raw:
        raise ReleaseResolutionError(
            "honest_path_unconfigured",
            f"{HONEST_PATH_ENV} must name the Honest rollout checkout",
            environment_variable=HONEST_PATH_ENV,
        )
    return Path(raw).expanduser().resolve()



def _confined_asset(root: Path, relative: str, *, rule: str, label: str) -> Path:
    """Resolve a governed Honest asset without permitting checkout escape."""
    raw = str(relative or "").strip()
    candidate = Path(raw)
    if not raw or candidate.is_absolute():
        raise ReleaseResolutionError(rule, f"{label} must be a relative path inside the configured Honest checkout", path=raw)
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseResolutionError(rule, f"{label} escapes the configured Honest checkout", path=raw) from exc
    return resolved


def parse_dish_version(text: str) -> dict[str, str]:
    """Parse the deliberately tiny two-key ``DISH_VERSION`` format."""

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        if raw_line.count("=") != 1:
            raise ReleaseResolutionError(
                "dish_version_malformed",
                f"DISH_VERSION line {line_number} must be KEY=VALUE",
                line=line_number,
            )
        key, value = raw_line.split("=", 1)
        if key not in _VERSION_KEYS:
            raise ReleaseResolutionError(
                "dish_version_unknown_key",
                f"DISH_VERSION line {line_number} has unknown key {key!r}",
                line=line_number,
                key=key,
            )
        if key in values:
            raise ReleaseResolutionError(
                "dish_version_duplicate_key",
                f"DISH_VERSION contains duplicate key {key}",
                key=key,
            )
        if not value:
            raise ReleaseResolutionError(
                "dish_version_empty_value",
                f"DISH_VERSION key {key} has an empty value",
                key=key,
            )
        if value != value.strip():
            raise ReleaseResolutionError(
                "dish_version_malformed",
                f"DISH_VERSION value for {key} must not contain surrounding whitespace",
                key=key,
            )
        values[key] = value

    missing = [key for key in _VERSION_KEYS if key not in values]
    if missing:
        raise ReleaseResolutionError(
            "dish_version_missing_key",
            "DISH_VERSION is missing required key(s)",
            keys=missing,
        )
    return values


def _read_required_text(path: Path, *, rule: str, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReleaseResolutionError(rule, f"missing {label}: {path.name}") from exc
    except (OSError, UnicodeError) as exc:
        raise ReleaseResolutionError(
            rule, f"unable to read {label}: {path.name}"
        ) from exc
    if not text.strip():
        raise ReleaseResolutionError(rule, f"{label} is empty: {path.name}")
    return text


def _load_json(path: Path, *, missing_rule: str, malformed_rule: str) -> tuple[str, Any]:
    raw = _read_required_text(path, rule=missing_rule, label=path.name)
    try:
        return raw, json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseResolutionError(
            malformed_rule,
            f"invalid JSON in {path.name}",
            line=exc.lineno,
            column=exc.colno,
        ) from exc


def _validate_migration(
    metadata: Any,
    *,
    filename: str,
    protocol_version: str,
    schema_version: str,
) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise ReleaseResolutionError(
            "migration_malformed", f"{filename} must contain a JSON object"
        )
    required = {
        "migration_id",
        "from_schema_version",
        "to_schema_version",
        "protocol_version",
        "automatic",
        "description",
        "source_ids",
        "operations",
    }
    if set(metadata) != required:
        raise ReleaseResolutionError(
            "migration_malformed",
            f"{filename} has the wrong keys",
            missing=sorted(required - set(metadata)),
            unknown=sorted(set(metadata) - required),
        )
    if metadata["to_schema_version"] != schema_version:
        raise ReleaseResolutionError(
            "migration_version_mismatch",
            f"{filename} targets the wrong schema version",
            expected=schema_version,
            actual=metadata["to_schema_version"],
        )
    migration_protocol = metadata["protocol_version"]
    if not isinstance(migration_protocol, str) or not migration_protocol.strip():
        raise ReleaseResolutionError(
            "migration_malformed",
            f"{filename} has an invalid protocol version",
        )
    if metadata["automatic"] is not False:
        raise ReleaseResolutionError(
            "migration_malformed",
            f"{filename} must be explicitly non-automatic in V1",
        )
    for key in ("migration_id", "description"):
        if not isinstance(metadata[key], str) or not metadata[key].strip():
            raise ReleaseResolutionError(
                "migration_malformed", f"{filename} has an empty {key}"
            )
    for key in ("source_ids", "operations"):
        value = metadata[key]
        if not isinstance(value, list) or not value:
            raise ReleaseResolutionError(
                "migration_malformed", f"{filename} {key} must be a non-empty list"
            )
    return copy.deepcopy(metadata)


def resolve_release(
    worktree: str | os.PathLike[str] | None,
    *,
    protocol_role: str | None = None,
    include_migrations: bool = False,
) -> ResolvedRelease:
    """Resolve one supported current Honest protocol/schema pair.

    The checkout need not be a Git worktree. Only the requested stage protocol is
    loaded; migration files are read only for an explicit migration operation.
    """

    if worktree is None or not str(worktree).strip():
        raise ReleaseResolutionError(
            "honest_path_unconfigured",
            "an explicit Honest rollout path is required",
        )
    root = Path(worktree).expanduser().resolve()
    if not root.is_dir():
        raise ReleaseResolutionError(
            "honest_path_missing", f"Honest rollout path does not exist: {root}"
        )

    version_path = _confined_asset(root, DISH_VERSION_FILENAME, rule="honest_asset_outside_checkout", label="DISH_VERSION")
    try:
        version_text = version_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReleaseResolutionError(
            "dish_version_missing",
            f"DISH_VERSION missing in configured Honest checkout: {root}",
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise ReleaseResolutionError(
            "dish_version_unreadable", "unable to read DISH_VERSION"
        ) from exc
    versions = parse_dish_version(version_text)
    protocol_version = versions["PROTOCOL_VERSION"]
    schema_version = versions["SCHEMA_VERSION"]

    if protocol_version != SUPPORTED_PROTOCOL_VERSION:
        raise ReleaseResolutionError(
            "protocol_version_unsupported",
            "configured Honest protocol version is unsupported",
            expected=SUPPORTED_PROTOCOL_VERSION,
            actual=protocol_version,
        )
    if schema_version != SUPPORTED_TASK_SCHEMA_VERSION:
        raise ReleaseResolutionError(
            "schema_version_unsupported",
            "configured Honest schema version is unsupported",
            expected=SUPPORTED_TASK_SCHEMA_VERSION,
            actual=schema_version,
        )

    schema_text, raw_schema = _load_json(
        _confined_asset(root, TASK_SCHEMA_FILENAME, rule="honest_asset_outside_checkout", label="task schema"),
        missing_rule="schema_missing",
        malformed_rule="schema_malformed",
    )
    schema = validate_task_schema_shape(raw_schema, filename=TASK_SCHEMA_FILENAME)
    if schema["protocol_version"] != protocol_version:
        raise ReleaseResolutionError(
            "schema_protocol_version_mismatch",
            "task schema protocol_version disagrees with DISH_VERSION",
            dish_version=protocol_version,
            schema=schema["protocol_version"],
        )
    if schema["schema_version"] != schema_version:
        raise ReleaseResolutionError(
            "schema_version_mismatch",
            "task schema schema_version disagrees with DISH_VERSION",
            dish_version=schema_version,
            schema=schema["schema_version"],
        )

    protocols: dict[str, str] = {}
    if protocol_role is not None:
        if protocol_role not in PROTOCOL_FILENAMES:
            raise ReleaseResolutionError(
                "protocol_role_unknown",
                f"unknown protocol role: {protocol_role}",
                role=protocol_role,
            )
        filename = schema["protocol_files"][protocol_role]
        protocols[protocol_role] = _read_required_text(
            _confined_asset(root, filename, rule="honest_asset_outside_checkout", label=f"{protocol_role} protocol"),
            rule="protocol_missing",
            label=f"{protocol_role} protocol",
        )

    migrations: dict[str, dict[str, Any]] = {}
    if include_migrations:
        for relative in schema["migration_files"]:
            path = _confined_asset(root, relative, rule="honest_asset_outside_checkout", label="schema migration")
            _, raw_metadata = _load_json(
                path,
                missing_rule="migration_missing",
                malformed_rule="migration_malformed",
            )
            migrations[relative] = _validate_migration(
                raw_metadata,
                filename=relative,
                protocol_version=protocol_version,
                schema_version=schema_version,
            )

    adapters = {kind: ReadOnlyLegacyAdapter(value) for kind, value in schema["legacy_validation_adapter"].items()}
    adapter_texts = {
        kind: json.dumps(value.as_dict(), ensure_ascii=False, sort_keys=True)
        for kind, value in adapters.items()
    }
    return ResolvedRelease(
        version=protocol_version,
        commit="",
        root=root,
        protocols=protocols,
        manifests=adapters,
        manifest_texts=adapter_texts,
        schema_version=schema_version,
        schema=copy.deepcopy(schema),
        schema_text=schema_text,
        migration_metadata=migrations,
        requested_protocol_role=protocol_role,
    )


def _git(repo: Path, *args: str, preserve_output: bool = False) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise ReleaseResolutionError(
            "verification_release_unreachable",
            "recorded Verification protocol release is unreachable",
            stderr=stderr.strip(),
        ) from exc
    return completed.stdout if preserve_output else completed.stdout.strip()


def current_verification_protocol_release(
    worktree: str | os.PathLike[str],
    *,
    read_at: datetime | None = None,
) -> VerificationProtocolSnapshot:
    """Resolve the current exact Verification protocol identity."""

    root = Path(worktree).expanduser().resolve()
    path = root / PROTOCOL_FILENAMES["verification"]
    text = _read_required_text(
        path, rule="protocol_missing", label="verification protocol"
    )
    try:
        git_root = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
        relative = path.resolve().relative_to(git_root).as_posix()
        commit = _git(git_root, "log", "-1", "--format=%H", "--", relative)
        if commit:
            committed_text = _git(
                git_root,
                "show",
                f"{commit}:{relative}",
                preserve_output=True,
            )
            if committed_text == text:
                return VerificationProtocolSnapshot(
                    identity=commit,
                    text=text,
                    source="git",
                )
    except (ReleaseResolutionError, ValueError):
        pass

    timestamp = read_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    stamp = timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return VerificationProtocolSnapshot(
        identity=f"sha256:{digest}; read-at={stamp}",
        text=text,
        source="sha256",
    )


def resolve_verification_protocol(
    worktree: str | os.PathLike[str], recorded_release: str
) -> VerificationProtocolSnapshot:
    """Recover exact Verification text without consulting current compatibility."""

    root = Path(worktree).expanduser().resolve()
    path = root / PROTOCOL_FILENAMES["verification"]
    recorded = str(recorded_release or "").strip()

    git_match = _GIT_RELEASE_RE.fullmatch(recorded)
    if git_match:
        commit = git_match.group("commit")
        try:
            git_root = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
            relative = path.resolve().relative_to(git_root).as_posix()
        except (ReleaseResolutionError, ValueError) as exc:
            raise ReleaseResolutionError(
                "verification_release_unreachable",
                "recorded Verification Git release is unreachable",
                release=recorded,
            ) from exc
        text = _git(
            git_root,
            "show",
            f"{commit}:{relative}",
            preserve_output=True,
        )
        if not text.strip():
            raise ReleaseResolutionError(
                "verification_release_unreachable",
                "recorded Verification protocol text is empty",
                release=recorded,
            )
        return VerificationProtocolSnapshot(
            identity=recorded,
            text=text,
            source="git",
        )

    hash_match = _HASH_RELEASE_RE.fullmatch(recorded)
    if hash_match:
        try:
            datetime.fromisoformat(hash_match.group("read_at").replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReleaseResolutionError(
                "verification_release_malformed",
                "recorded Verification hash release has an invalid timestamp",
            ) from exc
        text = _read_required_text(
            path, rule="verification_release_unreachable", label="verification protocol"
        )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != hash_match.group("digest"):
            raise ReleaseResolutionError(
                "verification_release_unreachable",
                "recorded Verification hash no longer matches recoverable text",
                release=recorded,
            )
        return VerificationProtocolSnapshot(
            identity=recorded,
            text=text,
            source="sha256",
        )

    raise ReleaseResolutionError(
        "verification_release_malformed",
        "Verification protocol release must be a Git commit or canonical sha256/read-at value",
        release=recorded,
    )
