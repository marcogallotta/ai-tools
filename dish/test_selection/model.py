"""Schema and loading helpers for the Dish test-selection policy."""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

POLICY_PATH = Path(__file__).with_name("ownership.csv")
POLICY_SHARD_FORMAT = "dish-test-ownership-shards-v1"

CLASS_NAMES = {
    "1": "Documentation and isolated tests",
    "2": "Frontend",
    "3": "Ordinary Python/service logic",
    "4": "Authority and canonical identity",
    "5": "Recovery and filesystem behavior",
    "6": "Schema, ORM, and migrations",
    "7": "PostgreSQL concurrency and projection lifecycle",
    "8": "Release, cutover, dark launch, and import",
}


ALLOWED_LANES = {
    "PGlite primary",
    "PGlite quarantine",
    "SQLite database-boundary",
    "Stage A mutation sample",
    "default mutation sample",
    "exact changed test/module",
    "flake diagnostics",
    "focused authority/identity",
    "focused ordinary",
    "focused postgresql runtime",
    "focused recovery/persistence",
    "focused release/import/dark-launch",
    "focused schema/model/migration",
    "browser acceptance",
    "frontend static/tooling",
    "native PostgreSQL certification",
    "ordinary full suite",
    "smoke",
    "source acceptance",
}


class PolicyError(RuntimeError):
    """The policy cannot produce a trustworthy plan."""


def split_field(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(";") if part.strip())



@dataclass(frozen=True)
class PolicyRow:
    path: str
    kind: str
    primary_class: str
    domain_class_for_tests: str
    traits: tuple[str, ...]
    direct_owner_tests: tuple[str, ...]
    critical_contract_tests: tuple[str, ...]
    shared_infrastructure_scope: str
    consumer_lanes: tuple[str, ...]
    default_lanes: tuple[str, ...]
    conditional_escalations: tuple[str, ...]
    escalation_predicates: tuple[str, ...]

    @property
    def primary_class_name(self) -> str:
        return CLASS_NAMES[self.primary_class]

    @classmethod
    def from_mapping(cls, value: dict[str, str]) -> "PolicyRow":
        scalar_fields = {
            "path",
            "kind",
            "primary_class",
            "domain_class_for_tests",
            "shared_infrastructure_scope",
        }
        list_fields = {
            "traits",
            "direct_owner_tests",
            "critical_contract_tests",
            "consumer_lanes",
            "default_lanes",
            "conditional_escalations",
            "escalation_predicates",
        }
        kwargs: dict[str, object] = {name: value.get(name, "").strip() for name in scalar_fields}
        kwargs.update({name: split_field(value.get(name, "")) for name in list_fields})
        return cls(**kwargs)  # type: ignore[arg-type]


def _policy_from_mappings(rows: list[dict[str, str]], *, source: str) -> dict[str, PolicyRow]:
    policy_rows = [PolicyRow.from_mapping(row) for row in rows]
    result: dict[str, PolicyRow] = {}
    duplicates: list[str] = []
    for row in policy_rows:
        if row.path in result:
            duplicates.append(row.path)
        result[row.path] = row
    if duplicates:
        raise PolicyError(
            f"duplicate policy paths in {source}: " + ", ".join(sorted(set(duplicates)))
        )
    return result


def _index_shards(value: str, *, source: str) -> tuple[str, ...] | None:
    if not value.lstrip().startswith("{"):
        return None
    try:
        index = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"invalid test-selection shard index {source}: {exc}") from exc
    if index.get("format") != POLICY_SHARD_FORMAT or not isinstance(index.get("shards"), list):
        raise PolicyError(f"unsupported test-selection shard index {source}")
    shards = tuple(str(item) for item in index["shards"])
    if not shards or len(set(shards)) != len(shards):
        raise PolicyError(f"test-selection shard index {source} has missing or duplicate shards")
    for shard in shards:
        candidate = Path(shard)
        if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix != ".csv":
            raise PolicyError(f"unsafe test-selection shard path {shard!r} in {source}")
    return shards


def load_policy_mappings(path: Path | None = None) -> tuple[list[str], list[dict[str, str]]]:
    policy_path = (path or POLICY_PATH).resolve()
    try:
        value = policy_path.read_text(encoding="utf-8")
        shards = _index_shards(value, source=str(policy_path))
        if shards is None:
            reader = csv.DictReader(io.StringIO(value))
            return list(reader.fieldnames or []), list(reader)
        fields: list[str] | None = None
        rows: list[dict[str, str]] = []
        for shard in shards:
            shard_path = policy_path.parent / shard
            with shard_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                shard_fields = list(reader.fieldnames or [])
                if fields is None:
                    fields = shard_fields
                elif shard_fields != fields:
                    raise PolicyError(f"test-selection shard columns differ in {shard_path}")
                rows.extend(reader)
        return fields or [], rows
    except OSError as exc:
        raise PolicyError(f"cannot read test-selection policy {policy_path}: {exc}") from exc


def policy_source_paths(path: Path | None = None) -> tuple[Path, ...]:
    """Return every file whose bytes define the loaded policy."""
    policy_path = (path or POLICY_PATH).resolve()
    try:
        value = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"cannot read test-selection policy {policy_path}: {exc}") from exc
    shards = _index_shards(value, source=str(policy_path))
    if shards is None:
        return (policy_path,)
    return (policy_path, *(policy_path.parent / shard for shard in shards))


def load_policy(path: Path | None = None) -> dict[str, PolicyRow]:
    policy_path = (path or POLICY_PATH).resolve()
    _, rows = load_policy_mappings(policy_path)
    return _policy_from_mappings(rows, source=str(policy_path))


def load_policy_text(value: str, *, source: str) -> dict[str, PolicyRow]:
    return _policy_from_mappings(list(csv.DictReader(io.StringIO(value))), source=source)


def load_policy_index_text(
    value: str, *, source: str, shard_loader: Callable[[str], str]
) -> dict[str, PolicyRow]:
    shards = _index_shards(value, source=source)
    if shards is None:
        return load_policy_text(value, source=source)
    rows: list[dict[str, str]] = []
    expected_fields: list[str] | None = None
    for shard in shards:
        reader = csv.DictReader(io.StringIO(shard_loader(shard)))
        fields = list(reader.fieldnames or [])
        if expected_fields is None:
            expected_fields = fields
        elif fields != expected_fields:
            raise PolicyError(f"test-selection shard columns differ in {source}:{shard}")
        rows.extend(reader)
    return _policy_from_mappings(rows, source=source)
