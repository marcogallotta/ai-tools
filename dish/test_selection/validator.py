"""Structural validation for the current-HEAD Dish test-selection map."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .model import ALLOWED_LANES, CLASS_NAMES, POLICY_PATH, split_field

ALLOWED_KINDS = {
    "documentation",
    "test",
    "frontend-source",
    "generated-frontend",
    "production",
    "shared-test-infrastructure",
    "config-or-runner",
    "source-artifact",
}
ALLOWED_BASIS = {"direct_import", "explicit_contract", "transitive", "dynamic_manual", "none"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_INFRA_SCOPE = {
    "none",
    "narrow",
    "lane-local",
    "cross-lane",
    "generated-fixture",
    "global",
    "policy-data",
}
REQUIRED_FIELDS = {
    "path",
    "kind",
    "primary_class",
    "primary_class_name",
    "domain_class_for_tests",
    "traits",
    "ownership_summary",
    "ownership_basis",
    "ownership_confidence",
    "direct_owner_tests",
    "critical_contract_tests",
    "other_direct_consumers",
    "transitive_consumers",
    "shared_infrastructure_scope",
    "direct_consumer_files",
    "consumer_lanes",
    "default_lanes",
    "conditional_escalations",
    "escalation_predicates",
    "native_postgresql_default",
    "native_postgresql_required_when",
    "pglite_default",
    "pglite_useful_when",
    "full_suite_trigger",
    "classification_confidence",
    "notes",
}
REFERENCE_FIELDS = {
    "direct_owner_tests",
    "critical_contract_tests",
    "other_direct_consumers",
    "transitive_consumers",
    "direct_consumer_files",
}
TOP_LEVEL_FILES = {
    "README.md",
    "alembic.ini",
    "pytest.ini",
    "requirements.txt",
    "requirements-test.txt",
    "requirements-flake.txt",
    "dish",
    "dish-admin",
    "dish-service",
    "dish-reports.sql",
}
SCOPED_ROOTS = {
    "dish_tool",
    "dish_service",
    "dish_pg",
    "dish_shadow",
    "test_selection",
    "tests",
    "frontend",
    "deploy",
    "openapi",
    "scripts",
    "docs",
}
EXCLUDED_PARTS = {".venv", ".pytest_cache", ".test-artifacts", "node_modules", "__pycache__"}
PARENT_GUIDANCE = {"../AGENTS.md", "../CLAUDE.md", "../README.md"}
GLOBAL_INFRA_PATHS = {
    "tests/conftest.py",
    "pytest.ini",
    "requirements-test.txt",
    "requirements-flake.txt",
    "scripts/dish-pg-native-certification",
    "scripts/dish-pg-pglite",
    "scripts/dish-pg-acceptance",
    "scripts/dish-test-plan",
    "test_selection/planner.py",
    "test_selection/validator.py",
    "tests/flake_runner.py",
    "tests/flake_policy.py",
    "tests/mutation_runner.py",
    "tests/mutation_cases.py",
}
POLICY_DATA_PATH = "test_selection/ownership.csv"


@dataclass(frozen=True)
class ValidationResult:
    row_count: int
    expected_repo_paths: int
    collection_checked: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        status = "checked" if self.collection_checked else "not-checked"
        return (
            f"rows={self.row_count} expected_repo_paths={self.expected_repo_paths} "
            f"collection={status} errors={len(self.errors)} warnings={len(self.warnings)}"
        )


def _scoped_paths(repo: Path) -> set[str]:
    out: set[str] = set()
    for root_name in SCOPED_ROOTS:
        root = repo / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(repo).parts
            if any(part in EXCLUDED_PARTS or part.startswith(".") for part in rel_parts):
                continue
            out.add(path.relative_to(repo).as_posix())
    for name in TOP_LEVEL_FILES:
        if (repo / name).is_file():
            out.add(name)
    return out


def _file_exists(repo: Path, parent: Path, ref: str) -> bool:
    if ref.startswith("../"):
        return (parent / ref[3:]).is_file()
    return (repo / ref).is_file()


def _looks_like_test_file(path: str) -> bool:
    return (
        path.startswith("tests/") or path.startswith("frontend/tests/")
    ) and Path(path).suffix in {".py", ".js", ".mjs", ".json", ".txt"}


def validate_policy(
    *,
    repo_root: Path,
    parent_root: Path | None = None,
    policy_path: Path | None = None,
    collected_nodeids_path: Path | None = None,
) -> ValidationResult:
    repo = repo_root.resolve()
    parent = (parent_root or repo.parent).resolve()
    policy = (policy_path or POLICY_PATH).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    collected_modules: set[str] | None = None

    if collected_nodeids_path is not None:
        try:
            raw = collected_nodeids_path.read_text(encoding="utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                nodeids = [
                    line.strip()
                    for line in raw.splitlines()
                    if "::" in line and line.strip().startswith(("tests/", "frontend/tests/"))
                ]
            else:
                if not isinstance(parsed, list):
                    raise ValueError("JSON collection evidence must be a list of pytest node IDs")
                nodeids = [str(node) for node in parsed]
            collected_modules = {node.split("::", 1)[0] for node in nodeids if "::" in node}
            if not collected_modules:
                raise ValueError("collection evidence contains no pytest node IDs")
        except Exception as exc:  # noqa: BLE001 - validator must report malformed external evidence
            errors.append(f"could not read collected nodeids: {exc}")

    try:
        with policy.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            missing_fields = REQUIRED_FIELDS - fields
            if missing_fields:
                errors.append(f"missing required columns: {sorted(missing_fields)}")
            rows = list(reader)
    except OSError as exc:
        return ValidationResult(0, len(_scoped_paths(repo)), collected_modules is not None, (str(exc),), ())

    paths = [row.get("path", "") for row in rows]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for path in paths:
        if path in seen:
            duplicates.add(path)
        seen.add(path)
    if duplicates:
        errors.append(f"duplicate mapped paths: {sorted(duplicates)}")

    mapped_repo = {path for path in paths if path and not path.startswith("../")}
    expected_repo = _scoped_paths(repo)
    missing_map = sorted(expected_repo - mapped_repo)
    stale_map = sorted(mapped_repo - expected_repo)
    if missing_map:
        errors.append(f"unclassified in-scope repository paths ({len(missing_map)}): {missing_map[:30]}")
    if stale_map:
        errors.append(f"mapped paths outside current scope or deleted ({len(stale_map)}): {stale_map[:30]}")

    mapped_parent = {path for path in paths if path.startswith("../")}
    if mapped_parent != PARENT_GUIDANCE:
        errors.append(
            f"parent guidance mismatch: expected {sorted(PARENT_GUIDANCE)}, got {sorted(mapped_parent)}"
        )

    row_by_path = {row["path"]: row for row in rows if row.get("path")}
    for index, row in enumerate(rows, start=2):
        prefix = f"row {index} ({row.get('path', '<missing>')})"
        path = row.get("path", "")
        primary_class = row.get("primary_class", "")
        kind = row.get("kind", "")
        traits = set(split_field(row.get("traits", "")))
        lanes = set(split_field(row.get("default_lanes", "")))
        consumer_lanes = set(split_field(row.get("consumer_lanes", "")))

        if not path:
            errors.append(f"{prefix}: empty path")
            continue
        if not _file_exists(repo, parent, path):
            errors.append(f"{prefix}: mapped path does not exist")
        if primary_class not in CLASS_NAMES:
            errors.append(f"{prefix}: invalid primary_class {primary_class!r}")
        elif row.get("primary_class_name") != CLASS_NAMES[primary_class]:
            errors.append(f"{prefix}: class name does not match class {primary_class}")
        if row.get("domain_class_for_tests") not in CLASS_NAMES:
            errors.append(f"{prefix}: invalid domain_class_for_tests")
        if kind not in ALLOWED_KINDS:
            errors.append(f"{prefix}: invalid kind {kind!r}")
        if row.get("ownership_basis") not in ALLOWED_BASIS:
            errors.append(f"{prefix}: invalid ownership_basis")
        if row.get("ownership_confidence") not in ALLOWED_CONFIDENCE:
            errors.append(f"{prefix}: invalid ownership_confidence")
        if row.get("classification_confidence") not in ALLOWED_CONFIDENCE:
            errors.append(f"{prefix}: invalid classification_confidence")
        scope = row.get("shared_infrastructure_scope")
        if scope not in ALLOWED_INFRA_SCOPE:
            errors.append(f"{prefix}: invalid shared_infrastructure_scope")
        invalid_lanes = (lanes | consumer_lanes) - ALLOWED_LANES
        if invalid_lanes:
            errors.append(f"{prefix}: invalid lanes {sorted(invalid_lanes)}")

        for field in REFERENCE_FIELDS:
            for ref in split_field(row.get(field, "")):
                if not _file_exists(repo, parent, ref):
                    errors.append(f"{prefix}: {field} references missing file {ref!r}")
                if field in {"direct_owner_tests", "critical_contract_tests"} and not _looks_like_test_file(ref):
                    errors.append(f"{prefix}: {field} must reference an exact test file, got {ref!r}")
                referenced_traits = set(
                    split_field(row_by_path.get(ref, {}).get("traits", ""))
                )
                if (
                    collected_modules is not None
                    and field in {"direct_owner_tests", "critical_contract_tests"}
                    and ref.startswith("tests/")
                    and Path(ref).name.startswith("test_")
                    and ref.endswith(".py")
                    and "quarantined" not in referenced_traits
                    and ref not in collected_modules
                ):
                    errors.append(
                        f"{prefix}: {field} test file was not present in supplied successful pytest collection: {ref!r}"
                    )

        if kind == "production" and row.get("ownership_basis") == "none":
            errors.append(f"{prefix}: production path has no ownership basis")
        if kind == "test":
            if path not in split_field(row.get("direct_owner_tests", "")):
                errors.append(f"{prefix}: isolated test must own itself")
            if not row.get("domain_class_for_tests"):
                errors.append(f"{prefix}: test lacks domain class")
        if kind == "config-or-runner" and path != POLICY_DATA_PATH:
            if not split_field(row.get("direct_owner_tests", "")) and not split_field(
                row.get("critical_contract_tests", "")
            ):
                errors.append(f"{prefix}: config or runner lacks an explicit contract test")

        if "/migrations/versions/" in path:
            if row.get("ownership_basis") != "dynamic_manual":
                errors.append(f"{prefix}: Alembic version module must use dynamic_manual ownership")
            if row.get("native_postgresql_default") != "Required":
                errors.append(f"{prefix}: migration version must require native PostgreSQL")
            if row.get("pglite_default") != "Required":
                errors.append(f"{prefix}: migration version must require PGlite")
            required = {
                "SQLite database-boundary",
                "PGlite primary",
                "native PostgreSQL certification",
            }
            if not required.issubset(lanes):
                errors.append(f"{prefix}: migration version lacks mandatory lanes {sorted(required - lanes)}")

        if "release-critical" in traits and primary_class == "8" and kind in {
            "production",
            "config-or-runner",
        }:
            required = {
                "smoke",
                "SQLite database-boundary",
                "Stage A mutation sample",
                "source acceptance",
                "native PostgreSQL certification",
            }
            if not required.issubset(lanes):
                errors.append(f"{prefix}: release-critical path lacks mandatory lanes {sorted(required - lanes)}")

        if {"locking-concurrency", "projection-lifecycle"} & traits:
            if row.get("native_postgresql_default") != "Required":
                errors.append(f"{prefix}: locking/projection trait must require native PostgreSQL")
        if "shared-test-infrastructure" in traits and scope == "none":
            errors.append(f"{prefix}: shared infrastructure trait lacks fan-out scope")
        if path in GLOBAL_INFRA_PATHS:
            if scope != "global":
                errors.append(f"{prefix}: global policy path must have global scope")
            if "ordinary full suite" not in lanes:
                errors.append(f"{prefix}: global policy path must include ordinary full suite")
        if scope in {"narrow", "lane-local", "cross-lane", "generated-fixture", "policy-data"}:
            if "ordinary full suite" in lanes:
                errors.append(f"{prefix}: bounded or policy-data infrastructure must not force full suite")
        if scope == "cross-lane" and not consumer_lanes:
            errors.append(f"{prefix}: cross-lane infrastructure lacks consumer lanes")
        if path == POLICY_DATA_PATH:
            if scope != "policy-data":
                errors.append(f"{prefix}: ownership map must use policy-data scope")
            if not row.get("escalation_predicates"):
                errors.append(f"{prefix}: ownership map lacks semantic change predicates")

        if row.get("native_postgresql_default") not in {"Required", "Conditional", "No"}:
            errors.append(f"{prefix}: invalid native_postgresql_default")
        if row.get("pglite_default") not in {"Required", "Useful", "Conditional", "No"}:
            errors.append(f"{prefix}: invalid pglite_default")
        if row.get("native_postgresql_default") in {"Required", "Conditional"} and not row.get(
            "native_postgresql_required_when"
        ):
            errors.append(f"{prefix}: native requirement lacks predicate")
        if row.get("pglite_default") in {"Required", "Useful", "Conditional"} and not row.get(
            "pglite_useful_when"
        ):
            errors.append(f"{prefix}: PGlite policy lacks predicate")
        if not row.get("full_suite_trigger"):
            errors.append(f"{prefix}: missing full_suite_trigger")

    for path in GLOBAL_INFRA_PATHS:
        if path in expected_repo and path not in row_by_path:
            errors.append(f"global policy path is not mapped: {path}")

    return ValidationResult(
        row_count=len(rows),
        expected_repo_paths=len(expected_repo),
        collection_checked=collected_modules is not None,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
