#!/usr/bin/env python3
"""Validate the rare issue-based repository-bundle mirror refresh request."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

TITLE = "repository-bundle mirror refresh"
MARKER_RE = re.compile(
    r"<!-- dish-repository-bundle-mirror-refresh:v1 sha=([0-9a-f]{40}) -->"
)
SHA_RE = re.compile(r"[0-9a-f]{40}")
TRUSTED_PERMISSIONS = {"admin", "maintain", "write"}


class RequestError(RuntimeError):
    pass


def _read_event(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RequestError(f"cannot read issue event: {exc}") from exc
    if not isinstance(payload, dict):
        raise RequestError("issue event must be a JSON object")
    return payload


def validate_request(event: dict[str, Any], *, permission: str, current_main_sha: str) -> dict[str, str]:
    if permission not in TRUSTED_PERMISSIONS:
        raise RequestError(f"request actor lacks repository write authority: {permission!r}")
    if not SHA_RE.fullmatch(current_main_sha):
        raise RequestError("current main SHA is invalid")
    if event.get("action") != "opened":
        raise RequestError("refresh request must be a newly opened issue")
    issue = event.get("issue")
    sender = event.get("sender")
    if not isinstance(issue, dict) or not isinstance(sender, dict):
        raise RequestError("issue/sender metadata is missing")
    if issue.get("pull_request") is not None:
        raise RequestError("refresh request must be an issue, not a pull request")
    if issue.get("title") != TITLE:
        raise RequestError("refresh request title mismatch")
    body = str(issue.get("body") or "").strip()
    match = MARKER_RE.fullmatch(body)
    if not match:
        raise RequestError("refresh request body must contain only the exact v1 marker")
    requested_sha = match.group(1)
    if requested_sha != current_main_sha:
        raise RequestError(
            f"requested SHA is stale or mismatched: requested {requested_sha}, current main {current_main_sha}"
        )
    actor = str(sender.get("login") or "")
    opener = issue.get("user")
    opener_login = str(opener.get("login") or "") if isinstance(opener, dict) else ""
    if not actor or actor != opener_login:
        raise RequestError("issue opener/sender identity mismatch")
    number = issue.get("number")
    if not isinstance(number, int) or number < 1:
        raise RequestError("issue number is invalid")
    return {"source_sha": requested_sha, "actor": actor, "issue_number": str(number)}


def _write_outputs(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key in ("source_sha", "actor", "issue_number"):
            handle.write(f"{key}={values[key]}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-path", required=True)
    parser.add_argument("--permission", required=True)
    parser.add_argument("--current-main-sha", required=True)
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)
    try:
        values = validate_request(
            _read_event(Path(args.event_path)),
            permission=args.permission,
            current_main_sha=args.current_main_sha,
        )
        _write_outputs(args.github_output, values)
    except RequestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(values, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
