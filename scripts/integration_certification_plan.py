#!/usr/bin/env python3
"""Build deterministic repository-level Integration certification plans.

The caller supplies the complete exact changed-path set. Dish paths are delegated to the
existing governed Dish selector; this adapter only owns repository-level classification and the
mapping from semantic lanes to the four certification execution groups.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DISH_ROOT = ROOT / "dish"
POLICY_PATH = ROOT / "ci" / "integration-certification-policy.json"
SCHEMA_PATH = ROOT / "ci" / "integration-certification-plan.schema.json"

if str(DISH_ROOT) not in sys.path:
    sys.path.insert(0, str(DISH_ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from test_selection.model import ALLOWED_LANES as DISH_ALLOWED_LANES  # noqa: E402
from test_selection.model import PolicyError as DishPolicyError  # noqa: E402
from test_selection.planner import build_plan as build_dish_plan  # noqa: E402
from test_selection.planner import load_git_policy as load_dish_git_policy  # noqa: E402

import test_impact_graph as impact_graph  # noqa: E402

FORMAT = "repository-certification-plan-v2"
POLICY_IDENTITY_FORMAT = "repository-certification-policy-identity-v2"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_GROUPS = (
    "python-control-plane",
    "frontend-static",
    "native-postgresql",
    "browser-acceptance",
)
FUTURE_ADAPTER_LANES = {
    "browser acceptance",
    "frontend static",
    "frontend static/tooling",
    "repository control-plane",
}


class CertificationPlanError(RuntimeError):
    """The supplied inputs or repository policy cannot produce a trustworthy plan."""


@dataclass(frozen=True)
class RepositoryRule:
    name: str
    pattern: str
    root_only: bool
    lanes: tuple[str, ...]
    groups: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryPolicy:
    execution_groups: tuple[str, ...]
    full_certification_lanes: tuple[str, ...]
    lane_group_map: Mapping[str, str]
    force_full_patterns: tuple[tuple[str, str], ...]
    rules: tuple[RepositoryRule, ...]


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CertificationPlanError(f"cannot read certification policy {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CertificationPlanError(f"certification policy {path} must be a JSON object")
    return value


def load_repository_policy(path: Path = POLICY_PATH) -> RepositoryPolicy:
    raw = _read_json(path)
    if raw.get("format") != "repository-certification-policy-v1":
        raise CertificationPlanError("unknown repository certification policy format")

    groups = tuple(str(value) for value in raw.get("execution_groups", []))
    if groups != EXPECTED_GROUPS:
        raise CertificationPlanError(
            "repository certification policy execution_groups must be exactly: "
            + ", ".join(EXPECTED_GROUPS)
        )

    full_lanes = tuple(str(value) for value in raw.get("full_certification_lanes", []))
    lane_map_raw = raw.get("lane_group_map", {})
    if not isinstance(lane_map_raw, dict):
        raise CertificationPlanError("lane_group_map must be an object")
    lane_map = {str(lane): str(group) for lane, group in lane_map_raw.items()}
    invalid_mapped_groups = sorted(set(lane_map.values()) - set(groups))
    if invalid_mapped_groups:
        raise CertificationPlanError(
            "lane_group_map references unknown execution groups: "
            + ", ".join(invalid_mapped_groups)
        )

    force_patterns_raw = raw.get("force_full_patterns", [])
    if not isinstance(force_patterns_raw, list):
        raise CertificationPlanError("force_full_patterns must be an array")
    force_patterns: list[tuple[str, str]] = []
    for item in force_patterns_raw:
        if not isinstance(item, dict) or not item.get("pattern") or not item.get("reason"):
            raise CertificationPlanError("each force_full_patterns item needs pattern and reason")
        force_patterns.append((str(item["pattern"]), str(item["reason"])))

    rules_raw = raw.get("repository_rules", [])
    if not isinstance(rules_raw, list):
        raise CertificationPlanError("repository_rules must be an array")
    rules: list[RepositoryRule] = []
    for item in rules_raw:
        if not isinstance(item, dict) or not item.get("name") or not item.get("pattern"):
            raise CertificationPlanError("each repository rule needs name and pattern")
        rule_groups = tuple(str(value) for value in item.get("groups", []))
        invalid_groups = sorted(set(rule_groups) - set(groups))
        if invalid_groups:
            raise CertificationPlanError(
                f"repository rule {item['name']} references unknown groups: "
                + ", ".join(invalid_groups)
            )
        rules.append(
            RepositoryRule(
                name=str(item["name"]),
                pattern=str(item["pattern"]),
                root_only=bool(item.get("root_only", False)),
                lanes=tuple(str(value) for value in item.get("lanes", [])),
                groups=rule_groups,
            )
        )

    return RepositoryPolicy(
        execution_groups=groups,
        full_certification_lanes=full_lanes,
        lane_group_map=lane_map,
        force_full_patterns=tuple(force_patterns),
        rules=tuple(rules),
    )


def _canonical_sha(name: str, value: str) -> str:
    value = value.strip().lower()
    if not SHA_RE.fullmatch(value):
        raise CertificationPlanError(f"{name} must be an exact lowercase 40-hex commit SHA")
    return value


def _canonical_path(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise CertificationPlanError("changed paths must not be empty")
    if value.startswith(("/", "\\")) or "\\" in value:
        raise CertificationPlanError(f"changed path must be repository-relative POSIX form: {raw}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CertificationPlanError(f"changed path is not canonical repository-relative form: {raw}")
    return value


def normalize_changed_paths(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({_canonical_path(path) for path in paths}))


def _matches(path: str, pattern: str, *, root_only: bool = False) -> bool:
    if root_only and "/" in path:
        return False
    return fnmatch.fnmatchcase(path, pattern)


def _force_full_reasons_for_path(path: str, policy: RepositoryPolicy) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                f"{reason}:{path}"
                for pattern, reason in policy.force_full_patterns
                if _matches(path, pattern)
            }
        )
    )


def _repository_matches(path: str, policy: RepositoryPolicy) -> tuple[RepositoryRule, ...]:
    return tuple(
        rule
        for rule in policy.rules
        if _matches(path, rule.pattern, root_only=rule.root_only)
    )


def _group_for_lane(lane: str, policy: RepositoryPolicy) -> str | None:
    mapped = policy.lane_group_map.get(lane)
    if mapped:
        return mapped
    if lane in DISH_ALLOWED_LANES:
        return "python-control-plane"
    return None


def _policy_identity(repo_root: Path, policy_path: Path, schema_path: Path) -> dict[str, object]:
    sources = (
        policy_path,
        schema_path,
        Path(__file__).resolve(),
        repo_root / "scripts" / "test_impact_graph.py",
        repo_root / "scripts" / "test_impact_arbiter.py",
        repo_root / "ci" / "test-impact" / "targets.json",
        repo_root / "ci" / "test-impact" / "edges.json",
        repo_root / "ci" / "test-impact" / "replay.json",
        repo_root / "dish" / "test_selection" / "ownership.csv",
        repo_root / "dish" / "test_selection" / "model.py",
        repo_root / "dish" / "test_selection" / "planner.py",
    )
    records: list[dict[str, str]] = []
    combined = hashlib.sha256()
    for path in sources:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CertificationPlanError(f"cannot identify certification policy source {path}: {exc}") from exc
        try:
            relative = path.relative_to(repo_root).as_posix()
        except ValueError:
            relative = f"external/{path.name}"
        digest = hashlib.sha256(data).hexdigest()
        records.append({"path": relative, "sha256": digest})
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\n")
    return {
        "format": POLICY_IDENTITY_FORMAT,
        "combined_sha256": combined.hexdigest(),
        "sources": records,
    }


def _dish_selector_payload(plan: object | None) -> dict[str, object]:
    if plan is None:
        return {
            "format": "dish-test-plan-v1",
            "changed_paths": [],
            "classes": [],
            "traits": [],
            "focused_tests": [],
            "lanes": [],
            "conditional_reviews": [],
        }
    value = plan.as_dict()  # type: ignore[attr-defined]
    return {
        key: value[key]
        for key in (
            "format",
            "changed_paths",
            "classes",
            "traits",
            "focused_tests",
            "lanes",
            "conditional_reviews",
        )
    }


def _as_base_envelope(candidate: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(candidate)
    result["provenance"] = "base"
    for item in result["obligations"]:  # type: ignore[index]
        item["provenance"] = "base"
    return result


def _add_semantic_targets(
    graph_plan: dict[str, object], additions: Iterable[str], *, repo_root: Path
) -> None:
    policy = load_repository_policy()
    targets = impact_graph.load_targets(repo_root / "ci" / "test-impact" / "targets.json")
    selected = {str(item["id"]): item for item in graph_plan["selected_targets"]}  # type: ignore[index]
    hosted = {str(item["id"]): item for item in graph_plan["hosted_required_targets"]}  # type: ignore[index]
    for lane in additions:
        group = _group_for_lane(lane, policy)
        if group is None:
            group = "python-control-plane"
        target_id = impact_graph.ALL_FALLBACKS[group]
        if target_id in selected or target_id in hosted:
            continue
        target = dict(targets[target_id])
        target["selection_reasons"] = [f"review-semantic-addition:{lane}"]
        if graph_plan["profile"] in target["profiles"]:
            selected[target_id] = target
        else:
            hosted[target_id] = target
        for child_id in target.get("child_targets", []):
            child = dict(targets[str(child_id)])
            child["selection_reasons"] = [f"child-launch:{target_id}"]
            if graph_plan["profile"] in child["profiles"]:
                selected[str(child_id)] = child
            else:
                hosted[str(child_id)] = child
    graph_plan["selected_targets"] = [selected[key] for key in sorted(selected)]
    graph_plan["hosted_required_targets"] = [hosted[key] for key in sorted(hosted)]
    graph_plan["selected_groups"] = sorted(
        {str(item["execution_boundary"]) for item in selected.values()},
        key=impact_graph.BOUNDARIES.index,
    )
    fingerprint = graph_plan["impact_fingerprint"]
    assert isinstance(fingerprint, dict)
    all_targets = {**selected, **hosted}
    fingerprint["target_ids"] = sorted(all_targets)
    fingerprint["execution_boundaries"] = sorted(
        {str(item["execution_boundary"]) for item in all_targets.values()},
        key=impact_graph.BOUNDARIES.index,
    )
    fingerprint["guarantees"] = sorted({
        str(value) for item in all_targets.values() for value in item["guarantees"]
    })


def build_repository_plan(
    changed_paths: Iterable[str],
    *,
    candidate_sha: str,
    base_sha: str,
    merge_base_sha: str,
    semantic_additions: Iterable[str] = (),
    semantic_review_complete: bool = False,
    profile: str = "PR_EXACT_HEAD",
    input_mode: str = "exact_git_delta",
    base_obligations: object | None = None,
    candidate_obligations: object | None = None,
    base_paths: Iterable[str] | None = None,
    candidate_paths: Iterable[str] | None = None,
    arbiter_compatible: bool = True,
    base_arbiter_union: object | None = None,
    repo_root: Path = ROOT,
    policy_path: Path = POLICY_PATH,
    schema_path: Path = SCHEMA_PATH,
) -> dict[str, object]:
    candidate = _canonical_sha("candidate_sha", candidate_sha)
    base = _canonical_sha("base_sha", base_sha)
    merge_base = _canonical_sha("merge_base_sha", merge_base_sha)
    paths = normalize_changed_paths(changed_paths)
    if not paths:
        raise CertificationPlanError("complete changed-path set must contain at least one path")
    if input_mode not in {"exact_git_delta", "explicit_predicted_paths"}:
        raise CertificationPlanError("input_mode must be exact_git_delta or explicit_predicted_paths")

    additions = tuple(sorted(set(lane.strip() for lane in semantic_additions if lane.strip())))
    allowed_additions = set(DISH_ALLOWED_LANES) | FUTURE_ADAPTER_LANES
    invalid_additions = sorted(set(additions) - allowed_additions)
    if invalid_additions:
        raise CertificationPlanError(
            "unknown semantic certification lane additions: " + ", ".join(invalid_additions)
        )

    policy = load_repository_policy(policy_path)
    classifications: list[dict[str, str]] = []

    dish_paths = tuple(path[len("dish/") :] for path in paths if path.startswith("dish/"))
    dish_plan = None
    if dish_paths:
        try:
            fallback = load_dish_git_policy(repo_root=repo_root / "dish", ref=merge_base)
            dish_plan = build_dish_plan(dish_paths, fallback_policy=fallback)
        except DishPolicyError as exc:
            dish_plan = None
        dish_classification = "dish-selector" if dish_plan is not None else "dish-selector-failed-closed"
        classifications.extend(
            {"path": f"dish/{path}", "scope": "dish", "classification": dish_classification}
            for path in dish_paths
        )

    repository_paths = tuple(path for path in paths if not path.startswith("dish/"))
    for path in repository_paths:
        matches = _repository_matches(path, policy)
        if not matches:
            classifications.append(
                {"path": path, "scope": "repository", "classification": "unclassified"}
            )
            continue
        names = sorted({rule.name for rule in matches})
        if len(names) != 1:
            classifications.append(
                {"path": path, "scope": "repository", "classification": "ambiguous"}
            )
            continue
        classifications.append(
            {"path": path, "scope": "repository", "classification": names[0]}
        )
    if candidate_obligations is None:
        candidate_obligations = impact_graph.build_legacy_envelope(
            paths, provenance="candidate", repo_root=repo_root
        )
    if base_obligations is None and not (set(paths) & impact_graph.GRAPH_SELF_PATHS):
        base_obligations = _as_base_envelope(candidate_obligations)  # type: ignore[arg-type]
    try:
        graph_plan = impact_graph.build_graph_plan(
            paths,
            base_envelope=base_obligations,
            candidate_envelope=candidate_obligations,
            profile=profile,
            base_paths=base_paths,
            candidate_paths=candidate_paths,
            repo_root=repo_root,
            arbiter_compatible=arbiter_compatible,
            base_arbiter_union=base_arbiter_union,
        )
    except impact_graph.GraphError as exc:
        graph_plan = impact_graph.build_graph_plan(
            paths,
            base_envelope=None,
            candidate_envelope=candidate_obligations,
            profile=profile,
            repo_root=repo_root,
        )
        graph_plan["all_boundary_fallback_reasons"] = [f"graph-validation-failed:{exc}"]
    failed_closed_paths = [
        item["path"] for item in classifications
        if item["classification"] in {"unclassified", "ambiguous", "dish-selector-failed-closed"}
    ]
    if failed_closed_paths:
        graph_plan["all_boundary_fallback"] = True
        graph_plan["all_boundary_fallback_reasons"] = [
            f"unclassified-impact:{path}" for path in sorted(failed_closed_paths)
        ]
        graph_plan["impact_fingerprint"]["all_boundary_fallback"] = True  # type: ignore[index]
        failed = set(failed_closed_paths)
        for item in graph_plan["selector_classifications"]:  # type: ignore[index]
            if item["path"] in failed:
                item["classification"] = "TRUE_UNKNOWN_ALL_BOUNDARY"
                item["retained_boundaries"] = list(impact_graph.BOUNDARIES)
        graph_plan["selector_gaps"] = [
            item for item in graph_plan["selector_gaps"]  # type: ignore[index]
            if item["path"] not in failed
        ]
    graph_plan["impact_fingerprint"]["input_mode"] = input_mode  # type: ignore[index]
    _add_semantic_targets(graph_plan, additions, repo_root=repo_root)
    return {
        "format": FORMAT,
        "identity": {
            "candidate_sha": candidate,
            "base_sha": base,
            "merge_base_sha": merge_base,
        },
        "changed_paths": list(paths),
        "profile": profile,
        "classifications": sorted(classifications, key=lambda item: item["path"]),
        "dish_selector": _dish_selector_payload(dish_plan),
        "semantic_additions": {
            "review_complete": semantic_review_complete,
            "lanes": list(additions),
        },
        **graph_plan,
        "policy_identity": _policy_identity(repo_root, policy_path, schema_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="integration_certification_plan.py")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--merge-base-sha", required=True)
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="exact repository-relative changed path; repeat for the complete changed set",
    )
    parser.add_argument("--profile", choices=impact_graph.PROFILES, default="PR_EXACT_HEAD")
    parser.add_argument(
        "--input-mode",
        choices=("exact_git_delta", "explicit_predicted_paths"),
        default="exact_git_delta",
    )
    parser.add_argument(
        "--semantic-add-lane",
        action="append",
        default=[],
        help="validated Review semantic lane addition; repeatable and additive only",
    )
    parser.add_argument(
        "--semantic-review-complete",
        action="store_true",
        help="Review explicitly resolved every selector conditional predicate for this exact head",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_repository_plan(
            args.path,
            candidate_sha=args.candidate_sha,
            base_sha=args.base_sha,
            merge_base_sha=args.merge_base_sha,
            semantic_additions=args.semantic_add_lane,
            semantic_review_complete=args.semantic_review_complete,
            profile=args.profile,
            input_mode=args.input_mode,
        )
    except CertificationPlanError as exc:
        print(f"integration-certification-plan: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
