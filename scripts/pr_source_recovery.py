#!/usr/bin/env python3
"""Prepare a fail-closed source-only inverse for an already-landed PR change.

This helper is Implementation tooling. It never rewrites or mutates ``main`` and it
never claims that database, deployment, runtime, or external effects were reversed.
The caller supplies the exact current-main identity already resolved from live GitHub.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pr_source_recovery_apply import apply_plan
from pr_source_recovery_planner import build_plan
from pr_source_recovery_types import RecoveryError, RecoveryPlan

__all__ = ["RecoveryError", "RecoveryPlan", "apply_plan", "build_plan", "main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr_source_recovery", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        command = sub.add_parser(name)
        command.add_argument("--repo-path", default=".")
        command.add_argument("--landed-sha", required=True)
        command.add_argument("--current-main-sha", required=True)
        command.add_argument("--current-main-ref", default="refs/remotes/origin/main")
        command.add_argument(
            "--known-residual-effect",
            action="append",
            default=[],
            help="known non-source effect still requiring its own recovery authority/evidence",
        )
        if name == "apply":
            command.add_argument("--expected-tree-sha")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        kwargs = dict(
            repo=Path(args.repo_path),
            landed_sha=args.landed_sha,
            current_main_sha=args.current_main_sha,
            current_main_ref=args.current_main_ref,
            known_residual_effects=args.known_residual_effect,
        )
        if args.command == "plan":
            plan = build_plan(**kwargs)
        else:
            plan = apply_plan(**kwargs, expected_tree_sha=args.expected_tree_sha)
    except RecoveryError as exc:
        print(
            json.dumps(
                {"schema": "dish-source-recovery-plan-v1", "status": "error", "error": str(exc)},
                indent=2,
            )
        )
        return 2
    print(json.dumps(plan.json(), indent=2, sort_keys=True))
    return 0 if plan.status == "candidate" else 3


if __name__ == "__main__":
    raise SystemExit(main())
