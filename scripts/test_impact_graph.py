#!/usr/bin/env python3
"""Canonical affected-test target graph and legacy migration adapter."""
from __future__ import annotations

import argparse
import ast
import csv
import fnmatch
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DISH_ROOT = ROOT / "dish"
if str(DISH_ROOT) not in sys.path:
    sys.path.insert(0, str(DISH_ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from test_selection.model import PolicyError as DishPolicyError  # noqa: E402
from test_selection.planner import build_plan as build_dish_plan  # noqa: E402

import test_impact_arbiter as arbiter  # noqa: E402

TARGETS_PATH = ROOT / "ci" / "test-impact" / "targets.json"
EDGES_PATH = ROOT / "ci" / "test-impact" / "edges.json"
REPLAY_PATH = ROOT / "ci" / "test-impact" / "replay.json"
REPOSITORY_POLICY_PATH = ROOT / "ci" / "integration-certification-policy.json"
OWNERSHIP_PATH = DISH_ROOT / "test_selection" / "ownership.csv"
PROFILES = ("LOCAL_FAST", "PR_EXACT_HEAD", "POSTMERGE_FULL")
BOUNDARIES = (
    "python-control-plane",
    "frontend-static",
    "native-postgresql",
    "browser-acceptance",
)
ALL_FALLBACKS = {boundary: f"fallback:{boundary}" for boundary in BOUNDARIES}
RETIREMENT_REASONS = {
    "incidental_broad_coverage",
    "duplicate_of",
    "obsolete_contract",
    "replaced_semantic_model",
}
RUNNERS = {
    "dish-pytest", "repo-pytest", "tools-pytest", "repo-python-full",
    "native-postgresql", "frontend-static", "browser", "pglite",
    "mutation", "source-acceptance",
}
REQUIREMENTS = {"python", "node", "postgresql", "chromium", "subprocess", "network-loopback"}
GRAPH_SELF_PATHS = {
    "scripts/test_impact_graph.py",
    "scripts/test_impact_arbiter.py",
    "ci/test-impact/targets.json",
    "ci/test-impact/edges.json",
    "ci/integration-certification-plan.schema.json",
    "scripts/integration_certification_plan.py",
    "scripts/pr_certification.py",
}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class GraphError(ValueError):
    """The graph cannot prove a non-narrowing plan."""


def _value_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _trusted_base_arbiter_union(
    raw: object, *, base_envelope: object, candidate_envelope: object, paths: tuple[str, ...]
) -> dict[str, object]:
    """Mechanically bind a BASE-arbiter union to the exact independently produced envelopes."""
    if not isinstance(raw, dict) or raw.get("format") != "dish-test-obligation-union-v1":
        raise GraphError("BASE arbiter union format is invalid")
    if not isinstance(base_envelope, dict) or not isinstance(candidate_envelope, dict):
        raise GraphError("BASE arbiter union requires object envelopes")
    expected: dict[str, tuple[str, object]] = {
        "base": ("base", base_envelope),
        "candidate": ("candidate", candidate_envelope),
    }
    normalized: dict[str, dict[str, object]] = {}
    for label, (provenance, envelope) in expected.items():
        assert isinstance(envelope, dict)
        if envelope.get("format") != "dish-test-obligations-v1" or envelope.get("provenance") != provenance:
            raise GraphError(f"{label} obligation envelope identity is invalid")
        engine_identity = envelope.get("engine_identity")
        obligations = envelope.get("obligations")
        if not isinstance(engine_identity, str) or not _SHA_RE.fullmatch(engine_identity):
            raise GraphError(f"{label} obligation engine identity is invalid")
        if envelope.get("changed_paths") != list(paths) or not isinstance(obligations, list):
            raise GraphError(f"{label} obligation envelope does not cover the exact changed paths")
        if any(not isinstance(item, dict) or item.get("provenance") != provenance for item in obligations):
            raise GraphError(f"{label} obligation envelope has invalid provenance")
        normalized[label] = {
            "engine_identity": engine_identity,
            "obligations": obligations,
        }
    expected_obligations = [
        *normalized["base"]["obligations"],  # type: ignore[list-item]
        *normalized["candidate"]["obligations"],  # type: ignore[list-item]
    ]
    expected_obligations.sort(
        key=lambda item: (str(item["path"]), str(item["key"]), str(item["provenance"]))
    )
    if raw.get("base_engine_identity") != normalized["base"]["engine_identity"]:
        raise GraphError("BASE arbiter union is bound to the wrong BASE engine")
    if raw.get("candidate_engine_identity") != normalized["candidate"]["engine_identity"]:
        raise GraphError("BASE arbiter union is bound to the wrong candidate engine")
    if raw.get("base_obligation_digest") != _value_digest(normalized["base"]["obligations"]):
        raise GraphError("BASE arbiter union BASE obligation digest mismatch")
    if raw.get("candidate_obligation_digest") != _value_digest(normalized["candidate"]["obligations"]):
        raise GraphError("BASE arbiter union candidate obligation digest mismatch")
    if raw.get("obligations") != expected_obligations or raw.get("union_digest") != _value_digest(expected_obligations):
        raise GraphError("BASE arbiter union narrowed or changed the independent obligation sets")
    semantic_keys = sorted({(str(item["path"]), str(item["key"])) for item in expected_obligations})
    if raw.get("semantic_keys") != [list(value) for value in semantic_keys]:
        raise GraphError("BASE arbiter union semantic key set mismatch")
    return dict(raw)


def _replay_base_arbiter_union(
    base_envelope: object, candidate_envelope: object, *, paths: tuple[str, ...]
) -> dict[str, object]:
    """Build replay-only non-narrowing union evidence without candidate arbiter semantics.

    Historical replay is a simulator, not certification authority. For replay cases
    that name a graph self-change path, provide the same mechanically checkable
    union shape that production receives from the independently executed BASE
    arbiter. Never call candidate ``union_envelopes()`` to construct this fixture.
    """
    if not isinstance(base_envelope, dict) or not isinstance(candidate_envelope, dict):
        raise GraphError("replay union requires object BASE and candidate envelopes")
    base_obligations = base_envelope.get("obligations")
    candidate_obligations = candidate_envelope.get("obligations")
    if not isinstance(base_obligations, list) or not isinstance(candidate_obligations, list):
        raise GraphError("replay union requires obligation arrays")
    obligations = [*base_obligations, *candidate_obligations]
    obligations.sort(
        key=lambda item: (str(item["path"]), str(item["key"]), str(item["provenance"]))
    )
    semantic_keys = sorted({(str(item["path"]), str(item["key"])) for item in obligations})
    raw = {
        "format": "dish-test-obligation-union-v1",
        "base_engine_identity": base_envelope.get("engine_identity"),
        "candidate_engine_identity": candidate_envelope.get("engine_identity"),
        "base_obligation_digest": _value_digest(base_obligations),
        "candidate_obligation_digest": _value_digest(candidate_obligations),
        "union_digest": _value_digest(obligations),
        "semantic_keys": [list(value) for value in semantic_keys],
        "obligations": obligations,
    }
    return _trusted_base_arbiter_union(
        raw, base_envelope=base_envelope, candidate_envelope=candidate_envelope, paths=paths
    )


def _json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphError(f"cannot read graph input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GraphError(f"graph input {path} must be an object")
    return value


def canonical_path(raw: str) -> str:
    value = raw.strip()
    if (
        not value
        or value.startswith(("/", "\\"))
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise GraphError(f"path must be canonical repository-relative POSIX form: {raw}")
    return value


def normalize_paths(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({canonical_path(path) for path in paths}))


def _digest_files(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise GraphError(f"cannot identify graph input {relative}: {exc}") from exc
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def graph_identity() -> str:
    return _digest_files((
        Path(__file__).resolve(),
        ROOT / "scripts" / "test_impact_arbiter.py",
        TARGETS_PATH,
        EDGES_PATH,
        ROOT / "ci" / "integration-certification-plan.schema.json",
        ROOT / "scripts" / "integration_certification_plan.py",
        ROOT / "scripts" / "integration_certification.py",
        ROOT / "scripts" / "pr_certification.py",
        REPOSITORY_POLICY_PATH,
        OWNERSHIP_PATH,
        DISH_ROOT / "test_selection" / "model.py",
        DISH_ROOT / "test_selection" / "planner.py",
    ))


def _test_target(test: str) -> dict[str, object]:
    if test.startswith("tests/postgresql/native/"):
        runner, boundary, size, requirements = (
            "native-postgresql", "native-postgresql", "large", ["python", "postgresql", "subprocess"]
        )
    elif test.startswith("frontend/tests/"):
        runner, boundary, size, requirements = (
            "browser", "browser-acceptance", "large", ["python", "node", "chromium", "network-loopback"]
        )
    elif test.startswith("tests/postgresql/pglite/"):
        return {
            "id": "harness:pglite-nested-collection",
            "runner": "pglite",
            "selector": "full",
            "execution_boundary": "python-control-plane",
            "guarantees": ["pglite-collection"],
            "size": "large",
            "requirements": ["python", "node", "subprocess"],
            "profiles": ["PR_EXACT_HEAD", "POSTMERGE_FULL"],
        }
    else:
        runner, boundary, size, requirements = (
            "dish-pytest", "python-control-plane", "small", ["python"]
        )
    return {
        "id": f"{runner}:{test}",
        "runner": runner,
        "selector": test,
        "execution_boundary": boundary,
        "guarantees": [f"test-file:{test}"],
        "size": size,
        "requirements": requirements,
        "profiles": list(PROFILES if size == "small" else ("PR_EXACT_HEAD", "POSTMERGE_FULL")),
        "generated_from": "dish/test_selection/ownership.csv",
    }


def _module_for_path(path: str) -> str | None:
    if not path.endswith(".py"):
        return None
    value = path[:-3]
    if value.endswith("/__init__"):
        value = value[: -len("/__init__")]
    if value.startswith("dish/"):
        value = value[len("dish/") :]
    return value.replace("/", ".")


def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return set()
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _static_python_targets(
    source_path: str, targets: Mapping[str, Mapping[str, object]], repo_root: Path
) -> tuple[str, ...]:
    module = _module_for_path(source_path)
    if module is None:
        return ()
    selected: list[str] = []
    for target_id, target in targets.items():
        if target.get("runner") != "dish-pytest":
            continue
        selector = str(target.get("selector", "")).split("::", 1)[0]
        test_path = repo_root / "dish" / selector
        imported = _imports(test_path)
        if module in imported or any(value.startswith(module + ".") for value in imported):
            selected.append(target_id)
    return tuple(sorted(selected))


def _owned_test_paths(path: Path = OWNERSHIP_PATH) -> tuple[str, ...]:
    tests: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                for field in ("direct_owner_tests", "critical_contract_tests"):
                    tests.update(part.strip() for part in row.get(field, "").split(";") if part.strip())
                if row.get("kind", "").strip() == "test":
                    tests.add(row.get("path", "").strip())
    except OSError as exc:
        raise GraphError(f"cannot read ownership policy {path}: {exc}") from exc
    return tuple(sorted(test for test in tests if test))


def load_targets(path: Path = TARGETS_PATH) -> dict[str, dict[str, object]]:
    raw = _json(path)
    if raw.get("format") != "dish-test-target-catalog-v1":
        raise GraphError("unknown target catalog format")
    if raw.get("execution_boundaries") != list(BOUNDARIES):
        raise GraphError("target catalog execution_boundaries must be the canonical ordered set")
    items = raw.get("targets")
    if not isinstance(items, list):
        raise GraphError("target catalog targets must be an array")
    targets: dict[str, dict[str, object]] = {}
    for item in [*items, *(_test_target(test) for test in _owned_test_paths())]:
        if not isinstance(item, dict):
            raise GraphError("target entries must be objects")
        target_id = item.get("id")
        if not isinstance(target_id, str) or not target_id:
            raise GraphError("target id must be non-empty")
        if target_id in targets:
            if targets[target_id] == item:
                continue
            raise GraphError(f"duplicate target id {target_id}")
        boundary = item.get("execution_boundary")
        if boundary not in BOUNDARIES:
            raise GraphError(f"target {target_id} has unknown execution boundary")
        profiles = item.get("profiles")
        if not isinstance(profiles, list) or not profiles or set(profiles) - set(PROFILES):
            raise GraphError(f"target {target_id} has invalid profiles")
        if item.get("runner") not in RUNNERS:
            raise GraphError(f"target {target_id} has unknown runner")
        if not isinstance(item.get("selector"), str) or not item.get("selector"):
            raise GraphError(f"target {target_id} has an empty selector")
        if item.get("size") not in {"small", "medium", "large"}:
            raise GraphError(f"target {target_id} has invalid size")
        guarantees = item.get("guarantees")
        requirements = item.get("requirements")
        if not isinstance(guarantees, list) or not guarantees or any(not isinstance(value, str) or not value for value in guarantees):
            raise GraphError(f"target {target_id} must declare guarantees")
        if not isinstance(requirements, list) or set(requirements) - REQUIREMENTS:
            raise GraphError(f"target {target_id} has invalid requirements")
        fallback_for = item.get("fallback_for")
        if fallback_for is not None and fallback_for != boundary:
            raise GraphError(f"target {target_id} is not boundary-compatible with its fallback_for")
        targets[target_id] = dict(item)
    for boundary, target_id in ALL_FALLBACKS.items():
        if target_id not in targets or targets[target_id]["execution_boundary"] != boundary:
            raise GraphError(f"missing compatible fallback target for {boundary}")
    return targets


def load_target_retirements(path: Path = TARGETS_PATH) -> dict[str, dict[str, object]]:
    raw = _json(path)
    items = raw.get("target_retirements", [])
    if not isinstance(items, list):
        raise GraphError("target_retirements must be an array")
    result: dict[str, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise GraphError("target retirement must name an id")
        target_id = str(item["id"])
        if target_id in result:
            raise GraphError(f"duplicate target retirement {target_id}")
        replacements = item.get("replacement_targets", [])
        retired = item.get("retired")
        if bool(replacements) == bool(retired):
            raise GraphError(f"target retirement {target_id} must choose replacement_targets or retired")
        if retired:
            if (
                not isinstance(retired, dict)
                or retired.get("reason") not in RETIREMENT_REASONS
                or not str(retired.get("provenance", "")).strip()
            ):
                raise GraphError(f"target retirement {target_id} lacks allowed reason/provenance")
        result[target_id] = dict(item)
    return result


def load_edges(
    targets: Mapping[str, Mapping[str, object]], path: Path = EDGES_PATH
) -> tuple[dict[str, object], ...]:
    raw = _json(path)
    if raw.get("format") != "dish-test-impact-edges-v1" or not isinstance(raw.get("mappings"), list):
        raise GraphError("unknown impact-edge format")
    result: list[dict[str, object]] = []
    exact_seen: set[str] = set()
    for index, item in enumerate(raw["mappings"]):  # type: ignore[index]
        if not isinstance(item, dict):
            raise GraphError(f"mapping {index} must be an object")
        path_value, pattern = item.get("path"), item.get("pattern")
        if (path_value is None) == (pattern is None):
            raise GraphError(f"mapping {index} must name exactly one path or pattern")
        mode = item.get("mode")
        if mode not in {"augment", "authoritative"}:
            raise GraphError(f"mapping {index} has invalid mode")
        if pattern is not None and mode != "augment":
            raise GraphError("authoritative mappings must be exact-path-only in V1")
        if path_value is not None:
            path_value = canonical_path(str(path_value))
            if path_value in exact_seen:
                raise GraphError(f"duplicate exact mapping {path_value}")
            exact_seen.add(path_value)
        mapping_targets = item.get("targets")
        if not isinstance(mapping_targets, list) or any(target not in targets for target in mapping_targets):
            raise GraphError(f"mapping {index} references an unknown target")
        if mode == "authoritative" and not isinstance(item.get("legacy_dispositions"), list):
            raise GraphError(f"authoritative mapping {path_value} needs legacy_dispositions")
        result.append(dict(item))
    return tuple(result)


def _lane_boundary(lane: str, lane_map: Mapping[str, object]) -> str:
    mapped = lane_map.get(lane)
    if isinstance(mapped, str) and mapped in BOUNDARIES:
        return mapped
    return "python-control-plane"


def _matches(path: str, pattern: str, *, root_only: bool = False) -> bool:
    return (not root_only or "/" not in path) and fnmatch.fnmatchcase(path, pattern)


def _repository_legacy(path: str, policy: Mapping[str, object]) -> tuple[list[str], list[str], bool]:
    rules = policy.get("repository_rules")
    if not isinstance(rules, list):
        raise GraphError("repository policy rules must be an array")
    matched = [
        rule for rule in rules
        if isinstance(rule, dict)
        and _matches(path, str(rule.get("pattern", "")), root_only=bool(rule.get("root_only")))
    ]
    names = {str(rule.get("name")) for rule in matched}
    if not matched or len(names) != 1:
        return [], [], True
    lanes = sorted({str(lane) for rule in matched for lane in rule.get("lanes", [])})
    return [], lanes, False


def _dish_legacy(path: str) -> tuple[list[str], list[str], bool]:
    relative = path[len("dish/") :]
    try:
        plan = build_dish_plan((relative,))
    except DishPolicyError:
        return [], [], True
    return list(plan.focused_tests), list(plan.lanes), False


def build_legacy_envelope(
    changed_paths: Iterable[str], *, provenance: str, repo_root: Path = ROOT
) -> dict[str, object]:
    if provenance not in {"base", "candidate"}:
        raise GraphError("provenance must be base or candidate")
    paths = normalize_paths(changed_paths)
    targets = load_targets(repo_root / "ci" / "test-impact" / "targets.json")
    mappings = load_edges(targets, repo_root / "ci" / "test-impact" / "edges.json")
    policy = _json(repo_root / "ci" / "integration-certification-policy.json")
    lane_map = policy.get("lane_group_map")
    if not isinstance(lane_map, dict):
        raise GraphError("repository policy lane_group_map must be an object")
    full_lanes = [str(value) for value in policy.get("full_certification_lanes", [])]
    force_patterns = policy.get("force_full_patterns")
    if not isinstance(force_patterns, list):
        raise GraphError("repository policy force_full_patterns must be an array")
    obligations: list[dict[str, object]] = []
    for path in paths:
        if path.startswith("dish/"):
            tests, lanes, unknown = _dish_legacy(path)
        else:
            tests, lanes, unknown = _repository_legacy(path, policy)
        forced = any(
            isinstance(item, dict) and _matches(path, str(item.get("pattern", "")))
            for item in force_patterns
        )
        if unknown:
            lanes = list(full_lanes)
        elif forced:
            lanes = sorted(set(lanes) | set(full_lanes))
        for test in sorted(set(tests)):
            target = _test_target(test)
            target_id = str(target["id"])
            if target_id not in targets:
                targets[target_id] = target
            boundary = str(target["execution_boundary"])
            obligations.append({
                "path": path,
                "key": f"legacy-target:{target_id}",
                "guarantee": f"test-file:{test}",
                "execution_boundary": boundary,
                "preferred_targets": [target_id],
                "fallback_target": ALL_FALLBACKS[boundary],
                "provenance": provenance,
            })
        for lane in sorted(set(lanes)):
            boundary = _lane_boundary(lane, lane_map)
            obligations.append({
                "path": path,
                "key": f"legacy-boundary:{boundary}:{lane}",
                "guarantee": lane,
                "execution_boundary": boundary,
                "preferred_targets": [],
                "fallback_target": ALL_FALLBACKS[boundary],
                "provenance": provenance,
            })
        for mapping in _matching_edges(path, mappings):
            for target_id in mapping.get("targets", []):
                target = targets[str(target_id)]
                boundary = str(target["execution_boundary"])
                obligations.append({
                    "path": path,
                    "key": f"graph-target:{target_id}",
                    "guarantee": f"graph-target:{target_id}",
                    "execution_boundary": boundary,
                    "preferred_targets": [str(target_id)],
                    "fallback_target": ALL_FALLBACKS[boundary],
                    "provenance": provenance,
                })
        for target_id in _static_python_targets(path, targets, repo_root):
            target = targets[target_id]
            boundary = str(target["execution_boundary"])
            obligations.append({
                "path": path,
                "key": f"graph-target:{target_id}",
                "guarantee": f"static-python-import:{target_id}",
                "execution_boundary": boundary,
                "preferred_targets": [target_id],
                "fallback_target": ALL_FALLBACKS[boundary],
                "provenance": provenance,
            })
    obligations.sort(key=lambda item: (str(item["path"]), str(item["key"])))
    return {
        "format": arbiter.FORMAT,
        "provenance": provenance,
        "engine_identity": graph_identity(),
        "changed_paths": list(paths),
        "obligations": obligations,
    }


def _matching_edges(path: str, mappings: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    return tuple(
        mapping for mapping in mappings
        if mapping.get("path") == path
        or (
            isinstance(mapping.get("pattern"), str)
            and fnmatch.fnmatchcase(path, str(mapping["pattern"]))
        )
    )


def _all_boundary_plan(
    paths: tuple[str, ...], targets: Mapping[str, Mapping[str, object]], *,
    profile: str, reason: str, candidate_engine_identity: str,
) -> dict[str, object]:
    selected = set(ALL_FALLBACKS.values())
    return _assemble_plan(
        paths, targets, selected, {}, profile=profile,
        legacy_paths=set(paths), fallback_reasons={boundary: {reason} for boundary in BOUNDARIES},
        retired=[], graph_id=candidate_engine_identity, obligation_union=None,
        all_boundary_reasons=[reason],
    )


def _assemble_plan(
    paths: tuple[str, ...], targets: Mapping[str, Mapping[str, object]],
    selected_ids: set[str], selection_reasons: Mapping[str, set[str]], *, profile: str,
    legacy_paths: set[str], fallback_reasons: Mapping[str, set[str]],
    retired: list[dict[str, object]], graph_id: str, obligation_union: object,
    all_boundary_reasons: list[str],
) -> dict[str, object]:
    pending = list(selected_ids)
    while pending:
        parent_id = pending.pop()
        for child_id in targets[parent_id].get("child_targets", []):
            if child_id not in targets:
                raise GraphError(f"target {parent_id} references unknown child target {child_id}")
            if child_id not in selected_ids:
                selected_ids.add(str(child_id))
                pending.append(str(child_id))
            if isinstance(selection_reasons, defaultdict):
                selection_reasons[str(child_id)].add(f"child-launch:{parent_id}")
    selected: list[dict[str, object]] = []
    hosted: list[dict[str, object]] = []
    for target_id in sorted(selected_ids):
        target = dict(targets[target_id])
        target["selection_reasons"] = sorted(selection_reasons.get(target_id, {"boundary-fallback"}))
        if profile not in target["profiles"]:
            hosted.append(target)
        else:
            selected.append(target)
    selected_groups = sorted(
        {str(item["execution_boundary"]) for item in selected}, key=BOUNDARIES.index
    )
    fingerprint = {
        "format": "dish-impact-fingerprint-v1",
        "graph_identity": graph_id,
        "input_mode": "exact_git_delta",
        "paths": list(paths),
        "target_ids": sorted(selected_ids),
        "guarantees": sorted({str(value) for target_id in selected_ids for value in targets[target_id]["guarantees"]}),
        "execution_boundaries": sorted(
            {str(targets[target_id]["execution_boundary"]) for target_id in selected_ids},
            key=BOUNDARIES.index,
        ),
        "resource_classes": sorted({str(targets[target_id]["size"]) for target_id in selected_ids}),
        "boundary_fallbacks": sorted(fallback_reasons),
        "all_boundary_fallback": bool(all_boundary_reasons),
        "legacy_adapter_paths": sorted(legacy_paths),
    }
    return {
        "graph_identity": graph_id,
        "profile": profile,
        "selected_targets": selected,
        "hosted_required_targets": hosted,
        "selected_groups": selected_groups,
        "boundary_fallbacks": [
            {"execution_boundary": boundary, "reasons": sorted(reasons)}
            for boundary, reasons in sorted(fallback_reasons.items(), key=lambda item: BOUNDARIES.index(item[0]))
        ],
        "legacy_adapter_paths": sorted(legacy_paths),
        "retired_legacy_obligations": retired,
        "all_boundary_fallback": bool(all_boundary_reasons),
        "all_boundary_fallback_reasons": sorted(all_boundary_reasons),
        "obligation_union": obligation_union,
        "impact_fingerprint": fingerprint,
    }


def build_graph_plan(
    changed_paths: Iterable[str], *, base_envelope: object | None,
    candidate_envelope: object, profile: str = "PR_EXACT_HEAD",
    base_paths: Iterable[str] | None = None, candidate_paths: Iterable[str] | None = None,
    repo_root: Path = ROOT, arbiter_compatible: bool = True,
    base_arbiter_union: object | None = None,
) -> dict[str, object]:
    paths = normalize_paths(changed_paths)
    if profile not in PROFILES:
        raise GraphError(f"unknown execution profile {profile}")
    targets = load_targets(repo_root / "ci" / "test-impact" / "targets.json")
    retirements = load_target_retirements(repo_root / "ci" / "test-impact" / "targets.json")
    mappings = load_edges(targets, repo_root / "ci" / "test-impact" / "edges.json")
    self_change = bool(set(paths) & GRAPH_SELF_PATHS)
    if self_change:
        if not isinstance(candidate_envelope, dict):
            raise GraphError("candidate obligation envelope must be an object")
        candidate_engine_identity = str(candidate_envelope.get("engine_identity") or "")
        if not _SHA_RE.fullmatch(candidate_engine_identity):
            raise GraphError("candidate obligation engine identity is invalid")
    else:
        candidate_valid = arbiter.validate_envelope(candidate_envelope, expected_provenance="candidate")
        candidate_engine_identity = str(candidate_valid["engine_identity"])
    if base_envelope is None:
        reason = "base-engine-obligation-envelope-unavailable"
        return _all_boundary_plan(paths, targets, profile=profile, reason=reason, candidate_engine_identity=candidate_engine_identity)
    if self_change and not arbiter_compatible:
        return _all_boundary_plan(
            paths, targets, profile=profile, reason="base-arbiter-incompatible-with-self-change",
            candidate_engine_identity=candidate_engine_identity,
        )
    if self_change:
        if base_arbiter_union is None:
            return _all_boundary_plan(
                paths, targets, profile=profile, reason="base-arbiter-union-unavailable-for-self-change",
                candidate_engine_identity=candidate_engine_identity,
            )
        try:
            union = _trusted_base_arbiter_union(
                base_arbiter_union, base_envelope=base_envelope,
                candidate_envelope=candidate_envelope, paths=paths,
            )
        except GraphError as exc:
            return _all_boundary_plan(
                paths, targets, profile=profile, reason=f"base-arbiter-union-invalid:{exc}",
                candidate_engine_identity=candidate_engine_identity,
            )
    else:
        try:
            union = arbiter.union_envelopes(base_envelope, candidate_valid)
        except arbiter.ArbiterError as exc:
            return _all_boundary_plan(
                paths, targets, profile=profile, reason=f"obligation-union-failed:{exc}",
                candidate_engine_identity=candidate_engine_identity,
            )
    obligations_by_path: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in union["obligations"]:  # type: ignore[index]
        obligations_by_path[str(item["path"])].append(item)
    base_present = set(base_paths if base_paths is not None else paths)
    candidate_present = set(candidate_paths if candidate_paths is not None else paths)
    selected: set[str] = set()
    reasons: dict[str, set[str]] = defaultdict(set)
    legacy_paths: set[str] = set()
    fallback_reasons: dict[str, set[str]] = defaultdict(set)
    retired: list[dict[str, object]] = []
    for path in paths:
        matched = _matching_edges(path, mappings)
        authoritative = [item for item in matched if item.get("mode") == "authoritative"]
        if len(authoritative) > 1:
            raise GraphError(f"multiple authoritative mappings match {path}")
        for mapping in matched:
            for target_id in mapping.get("targets", []):
                selected.add(str(target_id))
                reasons[str(target_id)].add(f"graph-edge:{path}:{mapping.get('reason')}")
        obligations = obligations_by_path[path]
        graph_obligations = [item for item in obligations if str(item["key"]).startswith("graph-target:")]
        legacy_obligations = [item for item in obligations if str(item["key"]).startswith("legacy-")]
        for obligation in graph_obligations:
            preferred = [str(value) for value in obligation["preferred_targets"]]
            compatible = [
                target_id for target_id in preferred
                if target_id in targets
                and targets[target_id]["execution_boundary"] == obligation["execution_boundary"]
            ]
            if compatible:
                for target_id in compatible:
                    selected.add(target_id)
                    reasons[target_id].add(f"base-candidate-graph-obligation:{path}:{obligation['key']}")
            else:
                removed_id = preferred[0] if preferred else ""
                retirement = retirements.get(removed_id)
                if retirement is None:
                    raise GraphError(f"removed graph target {removed_id} lacks target_retirements disposition")
                replacements = retirement.get("replacement_targets", [])
                for target_id in replacements:
                    if target_id not in targets or targets[str(target_id)]["execution_boundary"] != obligation["execution_boundary"]:
                        raise GraphError(f"target retirement {removed_id} has incompatible replacement {target_id}")
                fallback = str(obligation["fallback_target"])
                if fallback not in targets or targets[fallback]["execution_boundary"] != obligation["execution_boundary"]:
                    return _all_boundary_plan(
                        paths, targets, profile=profile,
                        reason=f"same-boundary-fallback-unavailable:{path}:{obligation['key']}",
                        candidate_engine_identity=candidate_engine_identity,
                    )
                selected.add(fallback)
                reasons[fallback].add(f"removed-target-fallback:{path}:{removed_id}")
                fallback_reasons[str(obligation["execution_boundary"])].add(f"{path}:{obligation['key']}")
        if authoritative:
            mapping = authoritative[0]
            if path not in base_present or path not in candidate_present:
                raise GraphError(
                    f"authoritative path {path} must exist in both BASE and CANDIDATE; candidate-added/renamed destinations remain legacy"
                )
            dispositions = mapping.get("legacy_dispositions")
            assert isinstance(dispositions, list)
            disposition_by_key: dict[str, Mapping[str, object]] = {}
            for disposition in dispositions:
                if not isinstance(disposition, dict) or not isinstance(disposition.get("key"), str):
                    raise GraphError(f"authoritative path {path} has malformed disposition")
                key = str(disposition["key"])
                if key in disposition_by_key:
                    raise GraphError(f"authoritative path {path} has duplicate disposition {key}")
                disposition_by_key[key] = disposition
            obligation_keys = {str(item["key"]) for item in legacy_obligations}
            if set(disposition_by_key) != obligation_keys:
                missing = sorted(obligation_keys - set(disposition_by_key))
                extra = sorted(set(disposition_by_key) - obligation_keys)
                raise GraphError(f"authoritative path {path} disposition mismatch; missing={missing}, extra={extra}")
            for obligation in legacy_obligations:
                key = str(obligation["key"])
                disposition = disposition_by_key[key]
                kind = disposition.get("kind")
                if kind == "mapped":
                    replacements = disposition.get("targets")
                    if not isinstance(replacements, list) or not replacements:
                        raise GraphError(f"mapped disposition {key} must name replacement targets")
                    for target_id in replacements:
                        if target_id not in targets:
                            raise GraphError(f"mapped disposition {key} references unknown target {target_id}")
                        if targets[str(target_id)]["execution_boundary"] != obligation["execution_boundary"]:
                            raise GraphError(f"mapped disposition {key} crosses execution boundaries")
                        selected.add(str(target_id))
                        reasons[str(target_id)].add(f"legacy-disposition:{path}:{key}")
                elif kind == "retired":
                    if (
                        disposition.get("reason") not in RETIREMENT_REASONS
                        or not isinstance(disposition.get("provenance"), str)
                        or not str(disposition.get("provenance")).strip()
                    ):
                        raise GraphError(f"retired disposition {key} lacks allowed reason/provenance")
                    retired.append({"path": path, **dict(disposition)})
                else:
                    raise GraphError(f"disposition {key} has invalid kind")
        else:
            legacy_paths.add(path)
            for obligation in legacy_obligations:
                preferred = [str(value) for value in obligation["preferred_targets"]]
                compatible = [
                    target_id for target_id in preferred
                    if target_id in targets
                    and targets[target_id]["execution_boundary"] == obligation["execution_boundary"]
                ]
                if compatible:
                    for target_id in compatible:
                        selected.add(target_id)
                        reasons[target_id].add(f"legacy-adapter:{path}:{obligation['key']}")
                else:
                    fallback = str(obligation["fallback_target"])
                    if fallback not in targets or targets[fallback]["execution_boundary"] != obligation["execution_boundary"]:
                        return _all_boundary_plan(
                            paths, targets, profile=profile,
                            reason=f"same-boundary-fallback-unavailable:{path}:{obligation['key']}",
                            candidate_engine_identity=candidate_engine_identity,
                        )
                    selected.add(fallback)
                    reasons[fallback].add(f"legacy-fallback:{path}:{obligation['key']}")
                    fallback_reasons[str(obligation["execution_boundary"])].add(
                        f"{path}:{obligation['key']}"
                    )
    return _assemble_plan(
        paths, targets, selected, reasons, profile=profile, legacy_paths=legacy_paths,
        fallback_reasons=fallback_reasons, retired=retired, graph_id=candidate_engine_identity,
        obligation_union=union, all_boundary_reasons=[],
    )


def replay(repo_root: Path = ROOT) -> dict[str, object]:
    raw = _json(repo_root / "ci" / "test-impact" / "replay.json")
    if raw.get("format") != "dish-test-impact-replay-v1" or not isinstance(raw.get("cases"), list):
        raise GraphError("unknown replay corpus format")
    results: list[dict[str, object]] = []
    for case in raw["cases"]:  # type: ignore[index]
        if not isinstance(case, dict) or not isinstance(case.get("changed_paths"), list):
            raise GraphError("malformed replay case")
        paths = [str(value) for value in case["changed_paths"]]
        base = build_legacy_envelope(paths, provenance="base", repo_root=repo_root)
        candidate = build_legacy_envelope(paths, provenance="candidate", repo_root=repo_root)
        normalized_paths = normalize_paths(paths)
        replay_union = (
            _replay_base_arbiter_union(base, candidate, paths=normalized_paths)
            if set(normalized_paths) & GRAPH_SELF_PATHS
            else None
        )
        plan = build_graph_plan(
            paths, base_envelope=base, candidate_envelope=candidate,
            base_paths=paths, candidate_paths=paths, repo_root=repo_root,
            base_arbiter_union=replay_union,
        )
        boundaries = set(plan["impact_fingerprint"]["execution_boundaries"])  # type: ignore[index]
        target_ids = set(plan["impact_fingerprint"]["target_ids"])  # type: ignore[index]
        missing_boundaries = sorted(set(case.get("must_boundaries", [])) - boundaries)
        forbidden_boundaries = sorted(set(case.get("must_not_boundaries", [])) & boundaries)
        missing_targets = sorted(set(case.get("must_targets", [])) - target_ids)
        passed = not (missing_boundaries or forbidden_boundaries or missing_targets)
        results.append({
            "id": case.get("id"), "passed": passed,
            "missing_boundaries": missing_boundaries,
            "forbidden_boundaries": forbidden_boundaries,
            "missing_targets": missing_targets,
            "selected_boundaries": sorted(boundaries),
            "selected_targets": sorted(target_ids),
        })
    return {
        "format": "dish-test-impact-replay-result-v1",
        "graph_identity": graph_identity(),
        "passed": all(bool(item["passed"]) for item in results),
        "cases": results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="test_impact_graph.py")
    sub = parser.add_subparsers(dest="command", required=True)
    obligations = sub.add_parser("obligations")
    obligations.add_argument("--path", action="append", default=[], required=True)
    obligations.add_argument("--provenance", choices=("base", "candidate"), required=True)
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "obligations":
            result = build_legacy_envelope(args.path, provenance=args.provenance)
        else:
            result = replay()
    except (GraphError, arbiter.ArbiterError) as exc:
        print(f"test-impact-graph: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
