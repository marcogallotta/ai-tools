#!/usr/bin/env python3
"""Deterministic base->head code-quality ratchet for Dish implementation work."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ci_failure_fingerprint import causal_fingerprint  # noqa: E402
from pr_certification import exact_changed_paths  # noqa: E402
from code_quality_common import (  # noqa: E402
    JSON_RE, MARKER_RE, SCHEMA, GateError, _canonical, _changed_pairs, _digest,
    _file_findings, _git, _load_policy, _load_registry, _sha,
)
from code_quality_analyzers import _positive_deltas, _run_analyzers  # noqa: E402

def evaluate(repo: Path, *, target_base: str, head: str, task_gid: str, pr_number: int | None = None, correction_round: int = 0) -> tuple[dict[str, Any], dict[str, float]]:
    target_base, head = _sha(target_base, "target_base"), _sha(head, "head")
    comparison_base = _sha(_git(repo, "merge-base", target_base, head), "comparison_base")
    if _git(repo, "merge-base", "--is-ancestor", comparison_base, head, allow=(0, 1)) == "":
        proc = subprocess.run(["git", "merge-base", "--is-ancestor", comparison_base, head], cwd=repo, check=False)
        if proc.returncode != 0: raise GateError("comparison base is not an ancestor of head")
    changed = exact_changed_paths(repo, merge_base=comparison_base, candidate=head)
    policy, policy_source, policy_digest, bootstrap = _load_policy(repo, comparison_base, head)
    registry, registry_digest = _load_registry(repo, policy_source, policy)
    failures, signals = _file_findings(repo, comparison_base, head, _changed_pairs(repo, comparison_base, head), policy, registry)
    analyzers: dict[str, Any] = {}
    timings: dict[str, float] = {}
    if bool(policy.get("enabled")) and not bootstrap:
        analyzers, analyzer_failures, timings = _run_analyzers(repo, comparison_base, head, changed, policy)
        failures.extend(analyzer_failures)
    max_rounds = int(policy["max_quality_correction_rounds"])
    if correction_round > max_rounds and failures:
        outcome = "WAIVER_REQUIRED"
    elif not bool(policy.get("enabled")):
        outcome = "DISABLED"
    elif bootstrap:
        outcome = "BOOTSTRAP"
    elif failures:
        outcome = "FIX_REQUIRED"
    else:
        outcome = "PASS"
    normalized_analyzers = {name: {**value, "digest": _digest(value)} for name, value in sorted(analyzers.items())}
    result: dict[str, Any] = {
        "schema": SCHEMA, "repository": "marcogallotta/ai-tools", "task_gid": task_gid, "pr_number": pr_number,
        "target_base_sha": target_base, "head_sha": head, "comparison_base_sha": comparison_base, "ancestor_proof": True,
        "changed_paths": list(changed), "changed_paths_digest": _digest(list(changed)),
        "policy_source_sha": policy_source, "policy_digest": policy_digest, "registry_digest": registry_digest, "bootstrap": bootstrap,
        "effective_enabled": bool(policy.get("enabled")),
        "tool_policy": {"ruff": policy["ruff"], "pyright": policy["pyright"], "jscpd": policy["jscpd"]},
        "analyzers": normalized_analyzers, "findings": sorted(failures, key=lambda x: _canonical(x)), "signals": sorted(signals, key=lambda x: _canonical(x)),
        "correction_round": correction_round, "max_quality_correction_rounds": max_rounds, "outcome": outcome,
    }
    if failures:
        fp, identity = causal_fingerprint(owner_surface="code-quality", failure_surface="pre-review-gate", invariant=failures[0]["kind"], signature=_digest(failures))
        result["causal_fingerprint"], result["causal_identity"] = fp, identity
    result["result_digest"] = _digest(result)
    return result, timings


def render_comment(result: dict[str, Any]) -> str:
    digest = str(result.get("result_digest") or "")
    if digest != _digest({k: v for k, v in result.items() if k != "result_digest"}):
        raise GateError("result digest is invalid")
    head = _sha(str(result.get("head_sha") or ""), "result head")
    return f"<!-- dish-code-quality-result:v1 head={head} digest={digest} -->\n```json\n{json.dumps(result, sort_keys=True, separators=(',', ':'))}\n```\n\n— Dish Agent: Implementation | ChatGPT"


def extract_comment(
    body: str,
    *,
    expected_head: str,
    expected_target_base: str | None = None,
    expected_comparison_base: str | None = None,
    expected_pr_number: int | None = None,
) -> dict[str, Any]:
    markers = MARKER_RE.findall(body); blocks = JSON_RE.findall(body)
    if len(markers) != 1 or len(blocks) != 1: raise GateError("code-quality comment must contain one marker and one JSON result")
    marker_head, marker_digest = markers[0]
    if marker_head != expected_head: raise GateError("code-quality comment head is stale")
    result = json.loads(blocks[0])
    if result.get("schema") != SCHEMA or result.get("head_sha") != expected_head: raise GateError("code-quality result identity is invalid")
    if expected_target_base is not None and result.get("target_base_sha") != expected_target_base:
        raise GateError("code-quality result target base is invalid")
    if expected_comparison_base is not None and result.get("comparison_base_sha") != expected_comparison_base:
        raise GateError("code-quality result comparison base is invalid")
    if expected_pr_number is not None and result.get("pr_number") != expected_pr_number:
        raise GateError("code-quality result PR identity is invalid")
    supplied = str(result.get("result_digest") or "")
    expected = _digest({k: v for k, v in result.items() if k != "result_digest"})
    if supplied != expected or marker_digest != expected: raise GateError("code-quality result digest mismatch")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate_cmd = sub.add_parser("evaluate"); evaluate_cmd.add_argument("--repo", default="."); evaluate_cmd.add_argument("--target-base", required=True); evaluate_cmd.add_argument("--head", required=True); evaluate_cmd.add_argument("--task-gid", required=True); evaluate_cmd.add_argument("--pr-number", type=int); evaluate_cmd.add_argument("--correction-round", type=int, default=0); evaluate_cmd.add_argument("--output", required=True)
    comment_cmd = sub.add_parser("render-comment"); comment_cmd.add_argument("--result", required=True)
    verify_cmd = sub.add_parser("verify-comment"); verify_cmd.add_argument("--comment", required=True); verify_cmd.add_argument("--expected-head", required=True); verify_cmd.add_argument("--expected-target-base"); verify_cmd.add_argument("--expected-comparison-base"); verify_cmd.add_argument("--expected-pr-number", type=int); verify_cmd.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "evaluate":
            result, timings = evaluate(Path(args.repo).resolve(), target_base=args.target_base, head=args.head, task_gid=args.task_gid, pr_number=args.pr_number, correction_round=args.correction_round)
            Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps({"outcome": result["outcome"], "result_digest": result["result_digest"], "timings_seconds": timings}, sort_keys=True))
            return 0 if result["outcome"] in {"PASS", "BOOTSTRAP", "DISABLED"} else 1
        if args.command == "render-comment":
            print(render_comment(json.loads(Path(args.result).read_text(encoding="utf-8"))))
            return 0
        result = extract_comment(
            Path(args.comment).read_text(encoding="utf-8"),
            expected_head=_sha(args.expected_head, "expected_head"),
            expected_target_base=_sha(args.expected_target_base, "expected_target_base") if args.expected_target_base else None,
            expected_comparison_base=_sha(args.expected_comparison_base, "expected_comparison_base") if args.expected_comparison_base else None,
            expected_pr_number=args.expected_pr_number,
        )
        Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    except (GateError, json.JSONDecodeError, OSError) as exc:
        print(f"TOOLING_ERROR: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
