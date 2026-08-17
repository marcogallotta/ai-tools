#!/usr/bin/env python3
"""Request durable full Review for an already-merged PR after the bounded safety pass."""
from __future__ import annotations

import argparse
import json
import os

from pr_lifecycle import LifecycleEngine
from pr_lifecycle_support import AsanaREST, GitHubREST, LifecycleError, WorkspaceAgentDispatcher


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--repo", default="marcogallotta/ai-tools")
    value.add_argument("--pr-number", required=True, type=int)
    value.add_argument("--thin-result", required=True, choices=["SAFE ENOUGH", "SERIOUS DEFECT FOUND", "UNABLE TO DETERMINE"])
    value.add_argument("--thin-summary", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    github_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    asana_token = os.getenv("ASANA_ACCESS_TOKEN")
    if not github_token or not asana_token:
        print(json.dumps({"status": "error", "error": "GITHUB_TOKEN/GH_TOKEN and ASANA_ACCESS_TOKEN are required"}, indent=2))
        return 2
    workspace_token = os.getenv("DISH_WORKSPACE_AGENT_ACCESS_TOKEN") or ""
    review_trigger = os.getenv("DISH_REVIEW_API_TRIGGER_ID")
    engine = LifecycleEngine(GitHubREST(args.repo, github_token), asana=AsanaREST(asana_token))
    workspace = WorkspaceAgentDispatcher(access_token=workspace_token, review_trigger_id=review_trigger) if (workspace_token or review_trigger) else None
    try:
        result = engine.request_post_merge_review(
            pr_number=args.pr_number, thin_result=args.thin_result, thin_summary=args.thin_summary, workspace=workspace
        )
    except LifecycleError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
