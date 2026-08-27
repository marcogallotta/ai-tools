#!/usr/bin/env python3
"""Validate the unique trusted dependency-bundle locator for one exact commit."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

CONTEXT = "Dish / dependency bundle"
WORKFLOW = ".github/workflows/dependency-bundle-mirror.yml"
RUN_RE = re.compile(r"/actions/runs/(\d+)(?:/|$)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class LocatorError(ValueError):
    pass


def _object(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LocatorError(f"{path} must contain a JSON object")
    return value


def validate(
    *, status: dict[str, Any], run: dict[str, Any], artifacts: dict[str, Any],
    repository: str, default_branch: str, sha: str, bundle_id: str,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(sha):
        raise LocatorError("locator SHA must be exact lowercase 40-hex")
    if status.get("sha") != sha:
        raise LocatorError("combined status is not for the exact locator SHA")
    matches = [item for item in status.get("statuses", []) if item.get("context") == CONTEXT]
    successes = [item for item in matches if item.get("state") == "success"]
    if len(successes) != 1:
        raise LocatorError(f"expected exactly one successful {CONTEXT!r} status; found {len(successes)}")
    target = str(successes[0].get("target_url") or "")
    match = RUN_RE.search(target)
    if not match or int(match.group(1)) != int(run.get("id") or 0):
        raise LocatorError("status target does not identify the supplied workflow run")
    if not (
        run.get("event") in {"push", "workflow_dispatch"}
        and run.get("path") == WORKFLOW
        and run.get("head_branch") == default_branch
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and (run.get("repository") or {}).get("full_name") == repository
    ):
        # push is the post-land main locator; workflow_dispatch is the
        # candidate locator. Release mirrors cannot satisfy admission.
        raise LocatorError("workflow run identity is not a trusted dependency mirror")
    live = [
        item for item in artifacts.get("artifacts", [])
        if item.get("name") == bundle_id and item.get("expired") is False
    ]
    if len(live) != 1:
        raise LocatorError(f"expected exactly one live artifact {bundle_id!r}; found {len(live)}")
    return {"bundle_id": bundle_id, "locator_sha": sha, "run_id": int(run["id"]), "artifact_id": int(live[0]["id"])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-json", required=True); parser.add_argument("--run-json", required=True)
    parser.add_argument("--artifacts-json", required=True); parser.add_argument("--repository", required=True)
    parser.add_argument("--default-branch", required=True); parser.add_argument("--sha", required=True)
    parser.add_argument("--bundle-id", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(validate(status=_object(args.status_json), run=_object(args.run_json), artifacts=_object(args.artifacts_json), repository=args.repository, default_branch=args.default_branch, sha=args.sha, bundle_id=args.bundle_id), sort_keys=True))
        return 0
    except (LocatorError, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"dependency_bundle_locator: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
