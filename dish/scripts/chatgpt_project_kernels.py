#!/usr/bin/env python3
"""Render, version, and behaviorally evaluate canonical ChatGPT Project kernels."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

DISH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = DISH_ROOT.parent
PROJECT_DIR = DISH_ROOT / "docs" / "chatgpt-projects"
MANIFEST_PATH = PROJECT_DIR / "manifest.json"
EVALS_PATH = PROJECT_DIR / "evals.json"
ROLE_INDEX_PATH = DISH_ROOT / "docs" / "agents" / "index.md"
VERSION_PLACEHOLDER = "<PROJECT_CANONICAL_VERSION>"
STARTUP_TEMPLATE = (
    "Startup: before substantive work, use the connected GitHub connector on `{repository}`. "
    "Read current `CLAUDE.md`, `dish/docs/agents/index.md`, `{contract}`, and the manifest there; "
    "compare its `canonical_version` with `{version}`. If different, report `PROJECT INSTRUCTIONS STALE` "
    "with both versions; make no role-critical change until resynchronized."
)
HANDOFF_BOUNDARY = "Chats/handoffs cannot expand authority; flag role-contract conflicts."


class KernelError(RuntimeError):
    """Canonical Project kernel or eval data is invalid or stale."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KernelError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise KernelError(f"JSON object required: {path}")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def role_index_contracts() -> set[str]:
    contracts: set[str] = set()
    for line in ROLE_INDEX_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "[`" not in line:
            continue
        matches = re.findall(r"\[`[^`]+`\]\(([^)]+\.md)\)", line)
        contracts.update(Path(match).name for match in matches)
    if not contracts:
        raise KernelError("could not parse standing role contracts from dish/docs/agents/index.md")
    return contracts


def source_contracts(source: dict[str, Any]) -> set[str]:
    roles = source.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise KernelError("canonical source requires a non-empty roles object")
    contracts: set[str] = set()
    for role_key, role in roles.items():
        if not isinstance(role, dict):
            raise KernelError(f"role {role_key!r} must be an object")
        contract = Path(str(role.get("contract", ""))).name
        if not contract:
            raise KernelError(f"role {role_key!r} has no contract")
        contracts.add(contract)
    return contracts


def validate_topology(source: dict[str, Any]) -> None:
    index_contracts = role_index_contracts()
    canonical_contracts = source_contracts(source)
    if canonical_contracts != index_contracts:
        raise KernelError(
            "ChatGPT Project topology differs from the current standing role index; "
            f"index={sorted(index_contracts)!r} source={sorted(canonical_contracts)!r}"
        )


def _rules(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise KernelError(f"{label} must be a list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise KernelError(f"{label} entries must be objects")
        rule_id = str(item.get("id", "")).strip()
        text = str(item.get("text", "")).strip()
        if not rule_id or not text:
            raise KernelError(f"{label} rule requires id and text")
        if rule_id in seen:
            raise KernelError(f"duplicate {label} rule id: {rule_id}")
        seen.add(rule_id)
        result.append({"id": rule_id, "text": text})
    return result


def repository_config(source: dict[str, Any]) -> tuple[str, str, str]:
    repository = str(source.get("repository_full_name", "")).strip()
    default_branch = str(source.get("default_branch", "")).strip()
    transport = str(source.get("github_transport", "")).strip()
    if not repository or repository.count("/") != 1:
        raise KernelError("canonical source requires repository_full_name in owner/name form")
    if not default_branch:
        raise KernelError("canonical source requires default_branch")
    if not transport:
        raise KernelError("canonical source requires github_transport")
    return repository, default_branch, transport


def effective_rules(source: dict[str, Any], role_key: str) -> list[dict[str, str]]:
    role = source["roles"][role_key]
    shared = _rules(source.get("shared_rules"), "shared_rules")
    specific = _rules(role.get("rules"), f"roles.{role_key}.rules")
    ids = [rule["id"] for rule in shared + specific]
    duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
    if duplicates:
        raise KernelError(f"duplicate effective rule ids for {role_key}: {duplicates}")
    return shared + specific


def render_role_with_version(source: dict[str, Any], role_key: str, version: str) -> str:
    role = source["roles"][role_key]
    contract = role["contract"]
    project_name = role["project_name"]
    default_role = role["default_role"]
    compositions = role.get("allowed_compositions", [])
    if not isinstance(compositions, list):
        raise KernelError(f"roles.{role_key}.allowed_compositions must be a list")
    repository, default_branch, transport = repository_config(source)
    lines = [
        f"# {project_name}",
        "",
        f"PROJECT_ROLE: {default_role}",
        f"PROJECT_CANONICAL_VERSION: {version}",
        "CANONICAL_MANIFEST: dish/docs/chatgpt-projects/manifest.json",
        f"ROLE_CONTRACT: {contract}",
        f"PROJECT_REPOSITORY: {repository}",
        f"PROJECT_DEFAULT_BRANCH: {default_branch}",
        "",
        STARTUP_TEMPLATE.format(contract=contract, version=version, repository=repository),
        "",
        f"Role: **{default_role}**.",
    ]
    if compositions:
        lines.append("Allowed composition only when explicitly triggered by current authority:")
        lines.extend(f"- {item}" for item in compositions)
    else:
        lines.append("No implicit role composition is permitted.")
    lines.extend([HANDOFF_BOUNDARY, "", "High-consequence rules:"])
    lines.extend(f"- {rule['text']}" for rule in effective_rules(source, role_key))
    lines.append("")
    return "\n".join(lines)


def kernel_identity(source: dict[str, Any]) -> str:
    payload = bytearray()
    for role_key in sorted(source["roles"]):
        payload.extend(role_key.encode("utf-8"))
        payload.extend(b"\0")
        payload.extend(render_role_with_version(source, role_key, VERSION_PLACEHOLDER).encode("utf-8"))
        payload.extend(b"\0")
    return _sha256_bytes(bytes(payload))


def load_canonical() -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _read_json(MANIFEST_PATH)
    source_path = PROJECT_DIR / str(manifest.get("source_file", ""))
    if not source_path.is_file():
        raise KernelError(f"canonical source missing: {source_path}")
    source_digest = _sha256(source_path)
    if manifest.get("source_sha256") != source_digest:
        raise KernelError(
            "canonical source hash mismatch; update manifest before rendering: "
            f"{manifest.get('source_sha256')!r} != {source_digest!r}"
        )
    source = _read_json(source_path)
    if source.get("schema_version") != manifest.get("schema_version"):
        raise KernelError("manifest/source schema version mismatch")
    rendered_identity = kernel_identity(source)
    if manifest.get("kernel_identity_sha256") != rendered_identity:
        raise KernelError(
            "rendered kernel identity mismatch; every behaviorally meaningful rendering change "
            "must update manifest/version: "
            f"{manifest.get('kernel_identity_sha256')!r} != {rendered_identity!r}"
        )
    namespace = str(manifest.get("version_namespace", ""))
    expected_version = f"{namespace}-{rendered_identity[:12]}"
    if manifest.get("canonical_version") != expected_version:
        raise KernelError(
            "canonical_version must bind the version namespace to the rendered kernel identity: "
            f"expected {expected_version!r}"
        )
    return manifest, source


def render_role(manifest: dict[str, Any], source: dict[str, Any], role_key: str) -> str:
    return render_role_with_version(source, role_key, str(manifest["canonical_version"]))


def generated_paths(manifest: dict[str, Any], source: dict[str, Any]) -> dict[str, Path]:
    configured = manifest.get("generated_role_files")
    if not isinstance(configured, dict):
        raise KernelError("manifest.generated_role_files must be an object")
    if set(configured) != set(source["roles"]):
        raise KernelError("generated_role_files keys must exactly match canonical roles")
    return {role_key: PROJECT_DIR / str(configured[role_key]) for role_key in source["roles"]}


def render_all(*, check: bool) -> list[tuple[str, int]]:
    manifest, source = load_canonical()
    validate_topology(source)
    max_chars = int(manifest.get("max_kernel_chars", 0))
    if max_chars <= 0:
        raise KernelError("manifest.max_kernel_chars must be positive")
    results: list[tuple[str, int]] = []
    for role_key, path in generated_paths(manifest, source).items():
        text = render_role(manifest, source, role_key)
        if len(text) > max_chars:
            raise KernelError(f"{path.relative_to(REPO_ROOT)} is {len(text)} chars; max is {max_chars}")
        if check:
            if not path.is_file():
                raise KernelError(f"generated kernel missing: {path.relative_to(REPO_ROOT)}")
            if path.read_text(encoding="utf-8") != text:
                raise KernelError(f"generated kernel stale: {path.relative_to(REPO_ROOT)}")
        else:
            path.write_text(text, encoding="utf-8")
        results.append((role_key, len(text)))
    return results


REQUIRED_EVAL_IDS = {
    "stale-project-version", "live-authority-over-stale-memory", "review-exact-head-completion",
    "coordinator-pr-intake-automatic-review", "reviewed-head-movement-classification",
    "implementation-rejects-patch-only-completion", "integration-rejects-head-mismatch",
    "current-template-lookup", "handoff-conflicts-with-role-authority",
    "allowed-specialist-implementation-composition", "forbidden-implicit-role-expansion",
    "task-history-before-no-op", "valid-action-fallback", "no-valid-fallback",
    "cross-role-context-bleed",
    "publication-fully-published-local-certification",
    "publication-unsafe-governed-path-blocker",
    "publication-blocker-forbids-unsafe-shortcuts",
    "publication-completion-invalidates-prior-review",
    "publication-handoff-before-human-notification",
    "configured-repository-pr-routing",
}
ORACLE_FIELDS = {
    "expected", "failure", "expected_outcome", "required_actions", "forbidden_actions",
    "required_observations", "required_observations_by_role",
    "require_ordered_observations", "observation_link_field",
}


def _eval_payload() -> dict[str, Any]:
    payload = _read_json(EVALS_PATH)
    if payload.get("schema_version") != 3:
        raise KernelError("evals.schema_version must be 3")
    if payload.get("runner_protocol") != "dish-chatgpt-project-behavior-v2":
        raise KernelError("evals.runner_protocol must be dish-chatgpt-project-behavior-v2")
    if not isinstance(payload.get("scenarios"), list):
        raise KernelError("evals.scenarios must be a list")
    return payload


def _evals() -> list[dict[str, Any]]:
    return _eval_payload()["scenarios"]


def _validate_observation_pattern(value: Any, scenario_id: str) -> None:
    if not isinstance(value, dict):
        raise KernelError(f"eval {scenario_id} observation patterns must be objects")
    for field in ("kind", "operation"):
        if not str(value.get(field, "")).strip():
            raise KernelError(f"eval {scenario_id} observation pattern requires {field}")
    for matcher in ("equals", "contains"):
        candidate = value.get(matcher, {})
        if not isinstance(candidate, dict):
            raise KernelError(f"eval {scenario_id} observation {matcher} must be an object")


def validate_eval_contracts() -> list[str]:
    _, source = load_canonical()
    validate_topology(source)
    seen: set[str] = set()
    for scenario in _evals():
        if not isinstance(scenario, dict):
            raise KernelError("eval scenario must be an object")
        scenario_id = str(scenario.get("id", "")).strip()
        if not scenario_id or scenario_id in seen:
            raise KernelError(f"missing or duplicate eval id: {scenario_id!r}")
        seen.add(scenario_id)
        roles = scenario.get("roles")
        required_rules = scenario.get("required_rules")
        required_actions = scenario.get("required_actions")
        forbidden_actions = scenario.get("forbidden_actions")
        if not isinstance(roles, list) or not roles:
            raise KernelError(f"eval {scenario_id} requires roles")
        if not isinstance(required_rules, list) or not required_rules:
            raise KernelError(f"eval {scenario_id} requires required_rules")
        if not isinstance(required_actions, list) or not required_actions:
            raise KernelError(f"eval {scenario_id} requires required_actions")
        if not isinstance(forbidden_actions, list) or not forbidden_actions:
            raise KernelError(f"eval {scenario_id} requires forbidden_actions")
        if set(required_actions) & set(forbidden_actions):
            raise KernelError(f"eval {scenario_id} has actions that are both required and forbidden")
        for role_key in roles:
            if role_key not in source["roles"]:
                raise KernelError(f"eval {scenario_id} references unknown role {role_key!r}")
            rules = {rule["id"] for rule in effective_rules(source, role_key)}
            missing = sorted(set(required_rules) - rules)
            if missing:
                raise KernelError(f"eval {scenario_id} lacks rules for {role_key}: {missing}")
        for field in ("expected", "failure", "expected_outcome"):
            if not str(scenario.get(field, "")).strip():
                raise KernelError(f"eval {scenario_id} requires {field}")
        prompts = scenario.get("prompts")
        if prompts is None:
            if not str(scenario.get("prompt", "")).strip():
                raise KernelError(f"eval {scenario_id} requires prompt or prompts")
        elif not isinstance(prompts, dict) or set(prompts) != set(roles) or any(not str(prompts[r]).strip() for r in roles):
            raise KernelError(f"eval {scenario_id} prompts must exactly match roles with non-empty values")
        required_observations = scenario.get("required_observations", [])
        if not isinstance(required_observations, list):
            raise KernelError(f"eval {scenario_id} required_observations must be a list")
        by_role = scenario.get("required_observations_by_role", {})
        if not isinstance(by_role, dict) or not set(by_role).issubset(set(roles)):
            raise KernelError(f"eval {scenario_id} required_observations_by_role must reference scenario roles")
        for pattern in required_observations:
            _validate_observation_pattern(pattern, scenario_id)
        for role_key, patterns in by_role.items():
            if not isinstance(patterns, list): raise KernelError(f"eval {scenario_id} observations for {role_key} must be a list")
            for pattern in patterns: _validate_observation_pattern(pattern, scenario_id)
        for role_key in roles:
            role_observations = by_role.get(role_key, required_observations)
            if scenario.get("require_ordered_observations") and not role_observations:
                raise KernelError(f"eval {scenario_id} cannot order absent observations for {role_key}")
            if scenario.get("observation_link_field") and len(role_observations) < 2:
                raise KernelError(f"eval {scenario_id} observation_link_field requires multiple observations for {role_key}")
    missing = sorted(REQUIRED_EVAL_IDS - seen)
    extras = sorted(seen - REQUIRED_EVAL_IDS)
    if missing or extras:
        raise KernelError(f"eval scenario set mismatch: missing={missing} extras={extras}")
    return sorted(seen)


def _action_vocabulary(scenarios: list[dict[str, Any]]) -> list[str]:
    return sorted({str(v) for s in scenarios for k in ("required_actions", "forbidden_actions") for v in s[k]})


def _outcome_vocabulary(scenarios: list[dict[str, Any]]) -> list[str]:
    return sorted({str(s["expected_outcome"]) for s in scenarios})


def prepare_eval_bundle() -> dict[str, Any]:
    manifest, source = load_canonical()
    validate_eval_contracts()
    scenarios = _evals()
    cases: list[dict[str, Any]] = []
    for scenario in scenarios:
        for role_key in scenario["roles"]:
            kernel = render_role(manifest, source, role_key)
            case = {
                "case_id": f"{scenario['id']}::{role_key}",
                "scenario_id": scenario["id"],
                "role": role_key,
                "project_name": source["roles"][role_key]["project_name"],
                "kernel_sha256": _sha256_bytes(kernel.encode("utf-8")),
                "project_instructions": kernel,
                "prompt": str(scenario.get("prompts", {}).get(role_key, scenario.get("prompt", ""))),
            }
            if ORACLE_FIELDS & set(case):
                raise KernelError(f"prepared eval case exposes hidden oracle fields: {sorted(ORACLE_FIELDS & set(case))}")
            cases.append(case)
    return {
        "schema_version": 2,
        "runner_protocol": "dish-chatgpt-project-behavior-v2",
        "canonical_version": manifest["canonical_version"],
        "fresh_chat_requirement": (
            "Run every case in a newly created chat inside the named ChatGPT Project; "
            "do not reuse a chat or carry prior-case conversation context."
        ),
        "response_contract": {
            "instruction": (
                "Capture the assistant response separately from runner-observed tool evidence. "
                "Assistant-authored claims that an action occurred are not observations. The runner must "
                "record actual capability discovery, durable writes, and authoritative readbacks from its tool layer."
            ),
            "assistant_response_shape": {"outcome": "<outcome>", "actions": ["<action-code>"]},
            "outcome_vocabulary": _outcome_vocabulary(scenarios),
            "action_vocabulary": _action_vocabulary(scenarios),
            "runner_observation_shape": {
                "seq": "<positive integer>", "kind": "<event kind>", "operation": "<tool operation>",
                "method": "<optional transport>", "pr": "<optional PR number>",
                "connector": "<optional connector name>", "repository": "<optional owner/name>",
                "head_sha": "<optional exact head>", "write_id": "<optional durable write identity>",
                "verified": "<optional boolean>", "available_methods": ["<optional method>"],
                "unavailable_methods": ["<optional method>"],
            },
        },
        "cases": cases,
    }


def _case_oracles() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for scenario in _evals():
        for role_key in scenario["roles"]:
            result[f"{scenario['id']}::{role_key}"] = {
                "expected_outcome": str(scenario["expected_outcome"]),
                "required_actions": {str(v) for v in scenario["required_actions"]},
                "forbidden_actions": {str(v) for v in scenario["forbidden_actions"]},
                "required_observations": list(scenario.get("required_observations_by_role", {}).get(role_key, scenario.get("required_observations", []))),
                "require_ordered_observations": bool(scenario.get("require_ordered_observations", False)),
                "observation_link_field": str(scenario.get("observation_link_field", "")).strip(),
            }
    return result


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list):
        return expected in actual
    if isinstance(actual, str):
        return str(expected) in actual
    return actual == expected


def _observation_matches(observation: dict[str, Any], pattern: dict[str, Any]) -> bool:
    if observation.get("kind") != pattern.get("kind") or observation.get("operation") != pattern.get("operation"):
        return False
    for field, expected in pattern.get("equals", {}).items():
        if observation.get(field) != expected:
            return False
    for field, expected in pattern.get("contains", {}).items():
        if not _contains(observation.get(field), expected):
            return False
    return True


def _validate_observed_evidence(case_id: str, oracle: dict[str, Any], observations: Any) -> None:
    patterns = oracle["required_observations"]
    if observations is None:
        observations = []
    if not isinstance(observations, list):
        raise KernelError(f"behavior result {case_id} runner_observations must be a list")
    if patterns and not observations:
        raise KernelError(f"behavior eval failed for {case_id}: missing runner-observed evidence")
    normalized: list[dict[str, Any]] = []
    seqs: list[int] = []
    for value in observations:
        if not isinstance(value, dict):
            raise KernelError(f"behavior result {case_id} runner observations must be objects")
        seq = value.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise KernelError(f"behavior result {case_id} observation seq must be a positive integer")
        normalized.append(value)
        seqs.append(seq)
    if len(set(seqs)) != len(seqs):
        raise KernelError(f"behavior result {case_id} observation seq values must be unique")
    normalized.sort(key=lambda item: int(item["seq"]))
    forbidden_operations = oracle["forbidden_actions"]
    for observation in normalized:
        operation = str(observation.get("operation", "")).strip()
        if operation in forbidden_operations:
            raise KernelError(
                f"behavior eval failed for {case_id}: runner observed forbidden operation {operation!r}"
            )
    if not patterns:
        return
    matched: list[dict[str, Any]] = []
    search_start = 0
    for pattern in patterns:
        found_index = None
        candidate_range = range(search_start, len(normalized)) if oracle["require_ordered_observations"] else range(len(normalized))
        for idx in candidate_range:
            if _observation_matches(normalized[idx], pattern):
                found_index = idx
                break
        if found_index is None:
            raise KernelError(f"behavior eval failed for {case_id}: missing required runner observation {pattern!r}")
        matched.append(normalized[found_index])
        if oracle["require_ordered_observations"]:
            search_start = found_index + 1
    link_field = oracle["observation_link_field"]
    if link_field:
        link_values = [obs.get(link_field) for obs in matched if obs.get(link_field) not in (None, "")]
        if len(link_values) < 2 or len(set(map(str, link_values))) != 1:
            raise KernelError(
                f"behavior eval failed for {case_id}: required observations do not share {link_field}"
            )


def evaluate_behavior_results(payload: dict[str, Any]) -> list[str]:
    manifest, _ = load_canonical()
    validate_eval_contracts()
    if payload.get("schema_version") != 2:
        raise KernelError("behavior results schema_version must be 2")
    if payload.get("runner_protocol") != "dish-chatgpt-project-behavior-v2":
        raise KernelError("behavior results runner_protocol must be dish-chatgpt-project-behavior-v2")
    if payload.get("canonical_version") != manifest["canonical_version"]:
        raise KernelError(
            "behavior results were not produced against the current Project kernel version: "
            f"{payload.get('canonical_version')!r} != {manifest['canonical_version']!r}"
        )
    results = payload.get("results")
    if not isinstance(results, list):
        raise KernelError("behavior results require a results list")
    oracles = _case_oracles()
    by_case: dict[str, dict[str, Any]] = {}
    chat_ids: set[str] = set()
    vocabulary = set(_action_vocabulary(_evals()))
    for result in results:
        if not isinstance(result, dict):
            raise KernelError("behavior result entries must be objects")
        case_id = str(result.get("case_id", "")).strip()
        fresh_chat_id = str(result.get("fresh_chat_id", "")).strip()
        if case_id not in oracles or case_id in by_case:
            raise KernelError(f"unknown or duplicate behavior result case: {case_id!r}")
        if not fresh_chat_id or fresh_chat_id in chat_ids:
            raise KernelError(f"missing or reused fresh_chat_id for {case_id}: {fresh_chat_id!r}")
        chat_ids.add(fresh_chat_id)
        response = result.get("assistant_response")
        if not isinstance(response, dict):
            raise KernelError(f"behavior result {case_id} assistant_response must be an object")
        outcome = str(response.get("outcome", "")).strip()
        actions_value = response.get("actions")
        if not isinstance(actions_value, list) or not actions_value:
            raise KernelError(f"behavior result {case_id} requires non-empty actions")
        actions = {str(value).strip() for value in actions_value if str(value).strip()}
        unknown_actions = sorted(actions - vocabulary)
        if unknown_actions:
            raise KernelError(f"behavior result {case_id} uses unknown actions: {unknown_actions}")
        oracle = oracles[case_id]
        missing_actions = sorted(oracle["required_actions"] - actions)
        forbidden_actions = sorted(oracle["forbidden_actions"] & actions)
        if outcome != oracle["expected_outcome"] or missing_actions or forbidden_actions:
            raise KernelError(
                f"behavior eval failed for {case_id}: outcome={outcome!r} expected={oracle['expected_outcome']!r} "
                f"missing_actions={missing_actions} forbidden_actions={forbidden_actions}"
            )
        _validate_observed_evidence(case_id, oracle, result.get("runner_observations"))
        by_case[case_id] = result
    missing_cases = sorted(set(oracles) - set(by_case))
    if missing_cases:
        raise KernelError(f"behavior results missing fresh-chat cases: {missing_cases}")
    return sorted(by_case)


def run_fresh_chat_runner(command: str) -> dict[str, Any]:
    argv = shlex.split(command)
    if not argv:
        raise KernelError("runner command is empty")
    bundle = prepare_eval_bundle()
    results: list[dict[str, Any]] = []
    for case in bundle["cases"]:
        runner_input = dict(case)
        runner_input["fresh_chat_requirement"] = bundle["fresh_chat_requirement"]
        runner_input["response_contract"] = bundle["response_contract"]
        completed = subprocess.run(argv, input=json.dumps(runner_input, sort_keys=True), text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise KernelError(
                f"fresh-chat runner failed for {case['case_id']} with exit {completed.returncode}: {completed.stderr.strip()}"
            )
        try:
            runner_output = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise KernelError(f"fresh-chat runner returned invalid JSON for {case['case_id']}") from exc
        if not isinstance(runner_output, dict):
            raise KernelError(f"fresh-chat runner output must be an object for {case['case_id']}")
        results.append({
            "case_id": case["case_id"],
            "fresh_chat_id": runner_output.get("fresh_chat_id"),
            "assistant_response": runner_output.get("assistant_response"),
            "runner_observations": runner_output.get("runner_observations", []),
        })
    return {
        "schema_version": 2,
        "runner_protocol": "dish-chatgpt-project-behavior-v2",
        "canonical_version": bundle["canonical_version"],
        "results": results,
    }


def version_status(project_version: str) -> tuple[bool, str]:
    manifest, _ = load_canonical()
    canonical = str(manifest["canonical_version"])
    if project_version == canonical:
        return True, f"PROJECT INSTRUCTIONS CURRENT — {canonical}"
    return False, (
        "PROJECT INSTRUCTIONS STALE — "
        f"project={project_version} repository={canonical}; resynchronize from canonical role kernel"
    )


def command_check() -> None:
    manifest, source = load_canonical()
    validate_topology(source)
    render_results = render_all(check=True)
    eval_contracts = validate_eval_contracts()
    print(f"canonical_version={manifest['canonical_version']}")
    print(f"kernel_identity_sha256={manifest['kernel_identity_sha256']}")
    for role_key, count in render_results:
        print(f"PASS kernel {role_key}: {count} chars")
    for scenario_id in eval_contracts:
        print(f"PASS eval-contract {scenario_id}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="render canonical role kernels")
    render.add_argument("--check", action="store_true", help="fail instead of writing if generated files differ")
    sub.add_parser("check", help="validate manifest, topology, generated kernels, and eval contracts")
    prepare = sub.add_parser("prepare-eval", help="write fresh-chat behavioral eval cases without hidden oracles")
    prepare.add_argument("--output", required=True, type=Path)
    evaluate = sub.add_parser("eval", help="run or judge fresh-chat behavioral adherence evals")
    source = evaluate.add_mutually_exclusive_group(required=True)
    source.add_argument("--results", type=Path, help="judge recorded fresh-chat result bundle")
    source.add_argument("--runner-command", help="invoke this command once per case; each invocation must create a fresh chat")
    evaluate.add_argument("--save-results", type=Path, help="save runner-produced results before judging")
    version = sub.add_parser("version", help="compare a Project-declared canonical version to repository authority")
    version.add_argument("--project-version", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "render":
            for role_key, count in render_all(check=args.check):
                print(f"PASS {role_key}: {count} chars")
        elif args.command == "check":
            command_check()
        elif args.command == "prepare-eval":
            _write_json(args.output, prepare_eval_bundle())
            print(f"WROTE {args.output}")
        elif args.command == "eval":
            if args.results is not None:
                if args.save_results is not None:
                    raise KernelError("--save-results is valid only with --runner-command")
                payload = _read_json(args.results)
            else:
                payload = run_fresh_chat_runner(args.runner_command)
                if args.save_results is not None:
                    _write_json(args.save_results, payload)
            for case_id in evaluate_behavior_results(payload):
                print(f"PASS behavior {case_id}")
        elif args.command == "version":
            current, message = version_status(args.project_version)
            print(message)
            return 0 if current else 3
    except KernelError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
