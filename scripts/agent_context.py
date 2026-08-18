#!/usr/bin/env python3
"""Resolve Dish role context from existing canonical role/source authority.

This module is routing only. Policy meaning remains in the standing documents and
ChatGPT Project source; resolving a document never grants role or mutation authority.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE_INDEX_PATH = Path("dish/docs/agents/index.md")
PROJECT_SOURCE_PATH = Path("dish/docs/chatgpt-projects/source.json")
CONTRIBUTOR_BASE_PATH = "dish/docs/agents/contributor-base.md"
OPERATOR_CONTROL_PLANE_PATH = "OPERATOR_CONTROL_PLANE.md"
ROLE_LINK_RE = re.compile(r"\[`[^`]+`\]\((?P<path>[^)]+\.md)\)")


class ContextError(RuntimeError):
    pass


def _read_json(repo_root: Path, relative: str) -> dict[str, Any]:
    try:
        value = json.loads((repo_root / relative).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContextError(f"cannot read canonical context source {relative}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContextError(f"canonical context source must be a JSON object: {relative}")
    return value


def _read_text(repo_root: Path, relative: str) -> str:
    try:
        return (repo_root / relative).read_text(encoding="utf-8")
    except OSError as exc:
        raise ContextError(f"cannot read required context {relative}: {exc}") from exc


def _safe_path(repo_root: Path, raw: str, label: str) -> str:
    value = str(raw).strip()
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ContextError(f"{label} has invalid repository path {value!r}")
    resolved = (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ContextError(f"{label} escapes repository: {value!r}") from exc
    if not resolved.is_file():
        raise ContextError(f"{label} dependency does not exist: {value!r}")
    return value


def role_contracts(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Return role-key -> canonical standing contract from the role index table."""
    index = _read_text(repo_root, str(ROLE_INDEX_PATH))
    contracts: dict[str, str] = {}
    for line in index.splitlines():
        if not line.startswith("|"):
            continue
        match = ROLE_LINK_RE.search(line)
        if not match:
            continue
        raw = match.group("path")
        if raw.startswith("../"):
            continue
        key = Path(raw).stem.lower().replace("_", "-")
        contracts[key] = f"dish/docs/agents/{raw}"
    if not contracts:
        raise ContextError("could not parse standing role contracts from role index")
    return contracts


def _source(repo_root: Path) -> dict[str, Any]:
    return _read_json(repo_root, str(PROJECT_SOURCE_PATH))


def _source_roles(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    roles = source.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise ContextError("ChatGPT Project source requires roles")
    if any(not isinstance(value, dict) for value in roles.values()):
        raise ContextError("ChatGPT Project role declarations must be objects")
    return roles


def repository_modifying_roles(source: dict[str, Any]) -> set[str]:
    """Derive modifying-role applicability from existing role composition authority.

    Implementation and Integration are intrinsically repository-mutating standing roles.
    Specialist roles are included only when their canonical Project declaration explicitly
    composes repository implementation. No independently maintained role list is used.
    """
    roles = _source_roles(source)
    modifying = {role for role in ("implementation", "integration") if role in roles}
    for role, cfg in roles.items():
        compositions = cfg.get("allowed_compositions", [])
        if not isinstance(compositions, list):
            raise ContextError(f"roles.{role}.allowed_compositions must be a list")
        if any("repository implementation" in str(item).casefold() for item in compositions):
            modifying.add(role)
    return modifying


def _triggered_reads(raw: Any, label: str) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ContextError(f"{label} must be an object")
    out: dict[str, list[str]] = {}
    for trigger, locators in raw.items():
        key = str(trigger).strip()
        if not key or not isinstance(locators, list) or not locators:
            raise ContextError(f"{label} entries require a trigger and destinations")
        out[key] = [str(locator).strip() for locator in locators]
        if any(not locator for locator in out[key]):
            raise ContextError(f"{label}.{key} contains an empty destination")
    return out


def _declared_dependencies(source: dict[str, Any], role: str) -> tuple[dict[str, Any] | None, dict[str, list[str]]]:
    roles = _source_roles(source)
    if role not in roles:
        raise ContextError(f"unknown Dish role {role!r}")
    shared = source.get("context_dependencies", {})
    local = roles[role].get("context_dependencies", {})
    if not isinstance(shared, dict) or not isinstance(local, dict):
        raise ContextError("context_dependencies declarations must be objects")

    triggered: dict[str, list[str]] = {}
    for origin, label in (
        (shared, "context_dependencies.triggered_reads"),
        (local, f"roles.{role}.context_dependencies.triggered_reads"),
    ):
        for trigger, destinations in _triggered_reads(origin.get("triggered_reads"), label).items():
            existing = triggered.get(trigger)
            if existing is not None and existing != destinations:
                raise ContextError(f"conflicting triggered read {trigger!r} for {role}")
            triggered[trigger] = destinations

    # Preserve the existing legacy declaration shape while it is being migrated.
    action_specific = local.get("action_specific")
    for trigger, destinations in _triggered_reads(
        action_specific, f"roles.{role}.context_dependencies.action_specific"
    ).items():
        triggered.setdefault(trigger, destinations)

    preload = local.get("preload")
    if preload is not None and not isinstance(preload, dict):
        raise ContextError(f"roles.{role}.context_dependencies.preload must be an object")
    return preload, triggered


def _validate_locator(repo_root: Path, locator: str, label: str) -> str:
    if "#" not in locator:
        return _safe_path(repo_root, locator, label)
    path, heading = locator.split("#", 1)
    path = _safe_path(repo_root, path, label)
    heading = heading.strip()
    if not heading:
        raise ContextError(f"{label} requires an exact section heading")
    text = _read_text(repo_root, path)
    if f"## {heading}" not in text.splitlines():
        raise ContextError(f"{label} section does not exist: {locator!r}")
    return f"{path}#{heading}"


def section_text(repo_root: Path, locator: str) -> str:
    """Read exactly one declared whole file or bounded ## section."""
    if "#" not in locator:
        return _read_text(repo_root, locator)
    path, heading = locator.split("#", 1)
    text = _read_text(repo_root, path)
    lines = text.splitlines()
    marker = f"## {heading}"
    try:
        start = lines.index(marker)
    except ValueError as exc:
        raise ContextError(f"declared section disappeared: {locator}") from exc
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("## "):
            end = idx
            break
    return "\n".join(lines[start:end]).rstrip() + "\n"


def resolve_context(
    role: str,
    *,
    repo_root: Path = REPO_ROOT,
    trigger: str | None = None,
) -> dict[str, Any]:
    """Resolve startup/re-ground and optional action-triggered context for one role."""
    role = role.strip().lower().replace("_", "-")
    source = _source(repo_root)
    roles = _source_roles(source)
    contracts = role_contracts(repo_root)
    if set(contracts) != set(roles):
        raise ContextError(
            "role index and ChatGPT Project source topology differ: "
            f"index={sorted(contracts)} source={sorted(roles)}"
        )
    if role not in roles:
        raise ContextError(f"unknown Dish role {role!r}")

    source_contract = str(roles[role].get("contract", "")).strip()
    if source_contract != contracts[role]:
        raise ContextError(
            f"role {role} contract differs between role index and Project source: "
            f"{contracts[role]!r} != {source_contract!r}"
        )

    preload, triggered = _declared_dependencies(source, role)
    startup_paths: list[str] = [source_contract, OPERATOR_CONTROL_PLANE_PATH]
    modifying = repository_modifying_roles(source)
    if role in modifying:
        startup_paths.append(CONTRIBUTOR_BASE_PATH)

    if preload is not None:
        if preload.get("role_index_contracts") is not True:
            raise ContextError(
                f"roles.{role}.context_dependencies.preload must derive role contracts through the role index"
            )
        startup_paths.extend(contracts[key] for key in sorted(contracts))
        additional = preload.get("additional")
        if not isinstance(additional, list) or not additional:
            raise ContextError(f"roles.{role}.context_dependencies.preload.additional must be non-empty")
        startup_paths.extend(str(path) for path in additional)

    startup: list[str] = []
    for raw in startup_paths:
        path = _safe_path(repo_root, raw, f"startup context for {role}")
        if path not in startup:
            startup.append(path)

    requested: list[str] = []
    if trigger is not None:
        key = trigger.strip()
        if key not in triggered:
            raise ContextError(f"role {role} has no declared context trigger {key!r}")
        for idx, raw in enumerate(triggered[key]):
            requested.append(_validate_locator(repo_root, raw, f"roles.{role}.{key}[{idx}]"))

    return {
        "role": role,
        "contract": source_contract,
        "repository_modifying": role in modifying,
        "startup_paths": startup,
        "available_triggers": sorted(triggered),
        "trigger": trigger,
        "triggered_reads": requested,
        "declaration_source": str(PROJECT_SOURCE_PATH),
        "role_index": str(ROLE_INDEX_PATH),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--role", required=True)
    parser.add_argument("--trigger")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        result = resolve_context(args.role, repo_root=Path(args.repo_root).resolve(), trigger=args.trigger)
    except ContextError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
