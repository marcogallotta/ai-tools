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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DISH_ROOT = ROOT / "dish"
POLICY_PATH = ROOT / "ci" / "integration-certification-policy.json"
SCHEMA_PATH = ROOT / "ci" / "integration-certification-plan.schema.json"

if str(DISH_ROOT) not in sys.path:
    sys.path.insert(0, str(DISH_ROOT))

from test_selection.model import ALLOWED_LANES as DISH_ALLOWED_LANES  # noqa: E402
from test_selection.model import PolicyError as DishPolicyError  # noqa: E402
from test_selection.planner import build_plan as build_dish_plan  # noqa: E402
from test_selection.planner import load_git_policy as load_dish_git_policy  # noqa: E402

FORMAT = "repository-certification-plan-v1"
POLICY_IDENTITY_FORMAT = "repository-certification-policy-identity-v1"
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


def _native_postgresql_selection(
    *,
    selected_groups: set[str],
    force_full: bool,
    dish_plan: object | None,
) -> dict[str, object]:
    if "native-postgresql" not in selected_groups:
        return {"mode": "none", "test_files": [], "reason": "not-selected"}
    if force_full:
        return {"mode": "full", "test_files": [], "reason": "repository-plan-force-full"}

    if (
        dish_plan is not None
        and "native PostgreSQL certification" in dish_plan.lanes  # type: ignore[attr-defined]
        and dish_plan.native_postgresql_fully_bound  # type: ignore[attr-defined]
    ):
        test_files = list(dish_plan.native_postgresql_test_files)  # type: ignore[attr-defined]
        if test_files:
            return {
                "mode": "focused",
                "test_files": test_files,
                "reason": "dish-selector-native-bindings",
            }
    # Every native-triggering path/addition must be exactly bound before narrowing.
    return {"mode": "full", "test_files": [], "reason": "native-impact-without-test-binding"}


def build_repository_plan(
    changed_paths: Iterable[str],
    *,
    candidate_sha: str,
    base_sha: str,
    merge_base_sha: str,
    semantic_additions: Iterable[str] = (),
    semantic_review_complete: bool = False,
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

    policy = load_repository_policy(policy_path)
    additions = tuple(sorted(set(lane.strip() for lane in semantic_additions if lane.strip())))
    allowed_additions = set(DISH_ALLOWED_LANES) | FUTURE_ADAPTER_LANES
    invalid_additions = sorted(set(additions) - allowed_additions)
    if invalid_additions:
        raise CertificationPlanError(
            "unknown semantic certification lane additions: " + ", ".join(invalid_additions)
        )

    classifications: list[dict[str, str]] = []
    force_reasons: set[str] = set()
    selected_lanes: set[str] = set(additions)
    selected_groups: set[str] = set()

    for path in paths:
        force_reasons.update(_force_full_reasons_for_path(path, policy))

    dish_paths = tuple(path[len("dish/") :] for path in paths if path.startswith("dish/"))
    dish_plan = None
    if dish_paths:
        try:
            fallback = load_dish_git_policy(repo_root=repo_root / "dish", ref=merge_base)
            dish_plan = build_dish_plan(dish_paths, fallback_policy=fallback)
        except DishPolicyError as exc:
            force_reasons.add(f"dish-selector-failed-closed:{exc}")
        else:
            selected_lanes.update(dish_plan.lanes)
            ordinary_focused_tests = tuple(
                test
                for test in dish_plan.focused_tests
                if not test.startswith("frontend/tests/")
                and not test.startswith("tests/postgresql/native/")
                and not test.startswith("tests/postgresql/pglite/")
            )
            if ordinary_focused_tests:
                selected_groups.add("python-control-plane")
            if dish_plan.conditional_reviews and not semantic_review_complete:
                force_reasons.add("unresolved-dish-semantic-review")
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
            force_reasons.add(f"unclassified-repository-path:{path}")
            continue
        names = sorted({rule.name for rule in matches})
        if len(names) != 1:
            classifications.append(
                {"path": path, "scope": "repository", "classification": "ambiguous"}
            )
            force_reasons.add(f"ambiguous-repository-path:{path}:{','.join(names)}")
            continue
        classifications.append(
            {"path": path, "scope": "repository", "classification": names[0]}
        )
        for rule in matches:
            selected_lanes.update(rule.lanes)
            selected_groups.update(rule.groups)

    for lane in tuple(sorted(selected_lanes)):
        group = _group_for_lane(lane, policy)
        if group is None:
            force_reasons.add(f"unmapped-certification-lane:{lane}")
        else:
            selected_groups.add(group)

    force_full = bool(force_reasons)
    if force_full:
        selected_lanes.update(policy.full_certification_lanes)
        selected_groups.update(policy.execution_groups)

    group_order = {name: index for index, name in enumerate(policy.execution_groups)}
    return {
        "format": FORMAT,
        "identity": {
            "candidate_sha": candidate,
            "base_sha": base,
            "merge_base_sha": merge_base,
        },
        "changed_paths": list(paths),
        "classifications": sorted(classifications, key=lambda item: item["path"]),
        "dish_selector": _dish_selector_payload(dish_plan),
        "semantic_additions": {
            "review_complete": semantic_review_complete,
            "lanes": list(additions),
        },
        "selected_lanes": sorted(selected_lanes),
        "selected_groups": sorted(selected_groups, key=group_order.__getitem__),
        "native_postgresql": _native_postgresql_selection(
            selected_groups=selected_groups, force_full=force_full, dish_plan=dish_plan
        ),
        "force_full": force_full,
        "force_full_reasons": sorted(force_reasons),
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
        )
    except CertificationPlanError as exc:
        print(f"integration-certification-plan: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
