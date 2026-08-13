"""Schema and loading helpers for the Dish test-selection policy."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

POLICY_PATH = Path(__file__).with_name("ownership.csv")

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


def _policy_from_reader(reader: csv.DictReader, *, source: str) -> dict[str, PolicyRow]:
    rows = [PolicyRow.from_mapping(row) for row in reader]
    result: dict[str, PolicyRow] = {}
    duplicates: list[str] = []
    for row in rows:
        if row.path in result:
            duplicates.append(row.path)
        result[row.path] = row
    if duplicates:
        raise PolicyError(
            f"duplicate policy paths in {source}: " + ", ".join(sorted(set(duplicates)))
        )
    return result


def load_policy(path: Path | None = None) -> dict[str, PolicyRow]:
    policy_path = (path or POLICY_PATH).resolve()
    try:
        with policy_path.open(newline="", encoding="utf-8") as handle:
            return _policy_from_reader(csv.DictReader(handle), source=str(policy_path))
    except OSError as exc:
        raise PolicyError(f"cannot read test-selection policy {policy_path}: {exc}") from exc


def load_policy_text(value: str, *, source: str) -> dict[str, PolicyRow]:
    return _policy_from_reader(csv.DictReader(io.StringIO(value)), source=source)
