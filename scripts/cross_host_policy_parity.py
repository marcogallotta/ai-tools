#!/usr/bin/env python3
"""Validate protected Dish policy delivery across ChatGPT and local hosts.

This is a structural integrity check. It proves declared policy dependencies are
reachable through each supported bootstrap shape; it does not evaluate model
behavior and it does not create a second semantic policy registry.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import agent_context

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = "dish/docs/chatgpt-projects/source.json"
MANIFEST_PATH = "dish/docs/chatgpt-projects/manifest.json"
ROLE_INDEX_PATH = "dish/docs/agents/index.md"
STANDING_PATH = "dish/docs/agents/standing-invariants.json"
CONTRIBUTOR_PATH = "dish/docs/agents/contributor-base.md"
OPERATOR_PATH = "OPERATOR_CONTROL_PLANE.md"
REVIEW_CONTRACT = "dish/docs/agents/review.md"
CLAUDE_SETTINGS = ".claude/settings.json"
CODEX_HOOKS = "codex/hooks.json"
GROUNDING_HOOK = "hooks/agent-grounding"
FRICTION_GID = "1217443500915644"
DEBT_GID = "1217443501022227"
DEFERRED_REVIEW_OWNER = "asana:task:1217547171327342"


class ParityError(RuntimeError):
    pass


def _text(repo: Path, path: str) -> str:
    try:
        return (repo / path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ParityError(f"cannot read {path}: {exc}") from exc


def _json(repo: Path, path: str) -> dict[str, Any]:
    try:
        value = json.loads(_text(repo, path))
    except json.JSONDecodeError as exc:
        raise ParityError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ParityError(f"JSON object required: {path}")
    return value


def _roles(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    roles = source.get("roles")
    if not isinstance(roles, dict) or not roles or any(not isinstance(v, dict) for v in roles.values()):
        raise ParityError("Project source requires role objects")
    return roles


def _extension(standing: dict[str, Any]) -> dict[str, Any]:
    entries = standing.get("invariants")
    if not isinstance(entries, list):
        raise ParityError("standing invariant registry has no invariants list")
    base = next((entry for entry in entries if isinstance(entry, dict) and entry.get("id") == "repository-context-admission"), None)
    if not isinstance(base, dict):
        raise ParityError("repository-context-admission standing invariant is missing")
    extensions = base.get("delivery_extensions")
    if not isinstance(extensions, dict):
        raise ParityError("standing invariant delivery extensions are missing")
    extension = extensions.get("cross_host_grounding_parity")
    if not isinstance(extension, dict):
        raise ParityError("cross-host grounding/parity delivery extension is missing")
    return extension


def validate_extension(extension: dict[str, Any]) -> None:
    status = str(extension.get("status", "")).strip()
    if status not in {"active", "superseded"}:
        raise ParityError("cross-host parity extension must be active or superseded")
    if status == "superseded":
        supersession = extension.get("supersession")
        required = ("authority_type", "durable_ref", "decision", "effective_at")
        if not isinstance(supersession, dict) or any(not str(supersession.get(key, "")).strip() for key in required):
            raise ParityError("superseded cross-host parity extension requires durable explicit supersession")
        return

    expected = {
        "context_declaration_source": SOURCE_PATH,
        "role_topology_source": ROLE_INDEX_PATH,
        "local_context_resolver": "scripts/agent_context.py",
        "local_grounding_hook": GROUNDING_HOOK,
        "structural_validator": "scripts/cross_host_policy_parity.py",
        "behavioral_eval_source": "dish/docs/chatgpt-projects/evals.json",
    }
    for key, value in expected.items():
        if extension.get(key) != value:
            raise ParityError(f"cross-host parity metadata {key} differs from canonical delivery surface")
    if extension.get("host_specific_transport_differences_allowed") is not True:
        raise ParityError("host-specific transport differences must remain explicitly classified")
    if extension.get("applicability_must_be_derived_or_cross_validated") is not True:
        raise ParityError("cross-host applicability must be derived or cross-validated")
    if extension.get("behavioral_adherence_is_structural_validator_out_of_scope") is not True:
        raise ParityError("structural parity validator must not become a behavioral-model gate")

    protected = extension.get("protected_delivery")
    if not isinstance(protected, list) or not protected:
        raise ParityError("cross-host parity metadata requires protected delivery entries")
    by_id = {str(item.get("id", "")): item for item in protected if isinstance(item, dict)}
    expected_ids = {"operator-control-plane", "contributor-base-inheritance", "friction-and-code-debt-discovery"}
    if set(by_id) != expected_ids:
        raise ParityError(f"protected delivery set mismatch: {sorted(set(by_id) ^ expected_ids)}")
    if by_id["operator-control-plane"].get("canonical_source") != OPERATOR_PATH:
        raise ParityError("operator control-plane delivery lost canonical source")
    for item_id in ("contributor-base-inheritance", "friction-and-code-debt-discovery"):
        if by_id[item_id].get("canonical_source") != CONTRIBUTOR_PATH:
            raise ParityError(f"{item_id} delivery lost canonical contributor-base source")

    # Provisional Review/Assurance semantic content must not enter protected authority.
    protected_text = json.dumps(protected, sort_keys=True).casefold()
    forbidden = ("attempt_id", "lane a", "lane b", "candidate-binding", "candidate binding")
    leaked = [token for token in forbidden if token in protected_text]
    if leaked:
        raise ParityError(f"unaccepted Review/Assurance semantics leaked into protected parity metadata: {leaked}")
    deferred = extension.get("deferred")
    if not isinstance(deferred, list) or len(deferred) != 1:
        raise ParityError("Review/Assurance parity must remain one explicit deferred dependency")
    record = deferred[0]
    if not isinstance(record, dict) or record.get("owner") != DEFERRED_REVIEW_OWNER or record.get("status") != "not-accepted-do-not-promote":
        raise ParityError("Review/Assurance parity deferral no longer matches its canonical owner/state")


def _generated_kernels(repo: Path, source: dict[str, Any], manifest: dict[str, Any]) -> dict[str, str]:
    files = manifest.get("generated_role_files")
    roles = _roles(source)
    if not isinstance(files, dict) or set(files) != set(roles):
        raise ParityError("generated Project role map differs from canonical source roles")
    return {role: _text(repo, f"dish/docs/chatgpt-projects/{files[role]}") for role in roles}


def _rule(roles: dict[str, dict[str, Any]], role: str, rule_id: str) -> dict[str, Any]:
    rules = roles[role].get("rules")
    if not isinstance(rules, list):
        raise ParityError(f"roles.{role}.rules must be a list")
    matches = [item for item in rules if isinstance(item, dict) and item.get("id") == rule_id]
    if len(matches) != 1:
        raise ParityError(f"role {role} requires exactly one {rule_id} rule")
    return matches[0]


def _hook_commands(config: dict[str, Any], event: str) -> list[tuple[str | None, str]]:
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        raise ParityError("hook config requires hooks object")
    entries = hooks.get(event)
    if not isinstance(entries, list):
        raise ParityError(f"hook config requires {event} entries")
    out: list[tuple[str | None, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        matcher = str(entry.get("matcher")) if entry.get("matcher") is not None else None
        nested = entry.get("hooks")
        if not isinstance(nested, list):
            continue
        for hook in nested:
            if isinstance(hook, dict) and hook.get("command"):
                out.append((matcher, str(hook["command"])))
    return out


def validate_local_hook_config(repo: Path) -> None:
    for path in (CLAUDE_SETTINGS, CODEX_HOOKS):
        config = _json(repo, path)
        session = _hook_commands(config, "SessionStart")
        pretool = _hook_commands(config, "PreToolUse")
        session_grounding = [(matcher, command) for matcher, command in session if "agent-grounding" in command]
        pretool_grounding = [(matcher, command) for matcher, command in pretool if "agent-grounding" in command]
        if len(session_grounding) != 1 or session_grounding[0][0] is not None:
            raise ParityError(f"{path} must run agent-grounding for every SessionStart source")
        if len(pretool_grounding) != 1 or pretool_grounding[0][0] is not None:
            raise ParityError(f"{path} must run agent-grounding at every PreToolUse boundary")
        if any("agent-reground" in command for _matcher, command in session + pretool):
            raise ParityError(f"{path} bypasses the shared cross-session grounding wrapper")

    hook = _text(repo, GROUNDING_HOOK)
    required_tokens = (
        "agent-reground",
        "agent_context.py",
        "session_grounding",
        "last_tool_witness",
        "last_action_witness",
        "dish_action_trigger",
        "DISH_ACTION_TRIGGER",
    )
    missing = [token for token in required_tokens if token not in hook]
    if missing:
        raise ParityError(f"local grounding wrapper lacks required receipt/resolver mechanics: {missing}")


def validate_review_handoff_semantics(repo: Path, source: dict[str, Any], kernels: dict[str, str]) -> None:
    roles = _roles(source)
    rule = _rule(roles, "review", "review-integration-boundary")
    text = str(rule.get("text", ""))
    # Host wording may differ, but semantic ownership may not: a successful Review hands
    # off to Integration and Review itself never merges.
    if "hands off to Integration" not in text or "Review does not merge" not in text:
        raise ParityError("generated Review handoff semantics drift from standing Integration boundary")
    if text not in kernels["review"]:
        raise ParityError("generated Review kernel no longer carries its declared handoff semantics")
    contract = _text(repo, REVIEW_CONTRACT)
    for required in ("VERDICT: MERGE", "Review itself never merges", "INTEGRATION READY"):
        if required not in contract:
            raise ParityError(f"standing Review lifecycle vocabulary missing accepted semantic marker: {required}")


def validate(repo: Path = REPO_ROOT) -> dict[str, Any]:
    source = _json(repo, SOURCE_PATH)
    manifest = _json(repo, MANIFEST_PATH)
    standing = _json(repo, STANDING_PATH)
    extension = _extension(standing)
    validate_extension(extension)

    roles = _roles(source)
    contracts = agent_context.role_contracts(repo)
    if set(roles) != set(contracts):
        raise ParityError("role topology differs between Project source and canonical role index")
    modifying = agent_context.repository_modifying_roles(source)
    if not modifying or not {"implementation", "integration"}.issubset(modifying):
        raise ParityError("repository-modifying role derivation lost core modifying roles")

    kernels = _generated_kernels(repo, source, manifest)
    role_index = _text(repo, ROLE_INDEX_PATH)
    if "OPERATOR_CONTROL_PLANE.md" not in role_index:
        raise ParityError("role index no longer declares shared operator control plane for all roles")
    if "All repository-modifying roles inherit" not in role_index or "contributor-base.md" not in role_index:
        raise ParityError("role index no longer declares contributor-base inheritance")

    contributor = _text(repo, CONTRIBUTOR_PATH)
    for required in (FRICTION_GID, DEBT_GID, "notice -> dedupe -> log/update -> continue"):
        if required not in contributor:
            raise ParityError(f"contributor-base lost protected friction/debt behavior: {required}")

    for role in sorted(roles):
        try:
            resolved = agent_context.resolve_context(role, repo_root=repo)
        except agent_context.ContextError as exc:
            raise ParityError(str(exc)) from exc
        if OPERATOR_PATH not in resolved["startup_paths"]:
            raise ParityError(f"local {role} startup lost shared operator-control-plane context")
        if role in modifying and CONTRIBUTOR_PATH not in resolved["startup_paths"]:
            raise ParityError(f"local modifying role {role} lost inherited contributor-base context")
        kernel = kernels[role]
        if "then read `CLAUDE.md`, role index" not in kernel:
            raise ParityError(f"ChatGPT {role} kernel lost role-index startup delivery")
        if role in modifying:
            friction = _rule(roles, role, "repository-friction-capture")
            friction_text = str(friction.get("text", ""))
            if FRICTION_GID not in friction_text or DEBT_GID not in friction_text or friction_text not in kernel:
                raise ParityError(f"ChatGPT modifying role {role} lost friction/debt discovery delivery")
            try:
                triggered = agent_context.resolve_context(
                    role, repo_root=repo, trigger="friction / code-debt finding"
                )
            except agent_context.ContextError as exc:
                raise ParityError(f"local modifying role {role} cannot resolve friction/debt trigger: {exc}") from exc
            if not any(locator.startswith(CONTRIBUTOR_PATH + "#") for locator in triggered["triggered_reads"]):
                raise ParityError(f"role {role} friction/debt trigger no longer resolves contributor-base sections")

    validate_local_hook_config(repo)
    validate_review_handoff_semantics(repo, source, kernels)
    return {
        "roles": sorted(roles),
        "repository_modifying_roles": sorted(modifying),
        "protected_delivery": [item["id"] for item in extension["protected_delivery"]],
        "deferred_owner": DEFERRED_REVIEW_OWNER,
        "status": "PASS",
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        result = validate(Path(args.repo_root).resolve())
    except (ParityError, agent_context.ContextError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
