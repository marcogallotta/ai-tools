#!/usr/bin/env python3
"""Fail-closed helpers for Dish PR review discovery and exact-head integration gates."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_ORDINARY_CI_CONTEXT = "Dish / required ordinary CI"


class GateError(ValueError):
    """The supplied GitHub state does not satisfy a lifecycle gate."""


def _load_json(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"could not read JSON {path!r}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError(f"expected a JSON object in {path!r}")
    return value


def _state(pr: dict[str, Any]) -> str:
    return str(pr.get("state", "")).lower()


def _draft(pr: dict[str, Any]) -> bool:
    if "draft" in pr:
        return bool(pr["draft"])
    if "isDraft" in pr:
        return bool(pr["isDraft"])
    raise GateError("PR JSON is missing draft/isDraft")


def _head_sha(pr: dict[str, Any]) -> str:
    head = pr.get("head")
    if isinstance(head, dict) and head.get("sha"):
        return str(head["sha"])
    if pr.get("headRefOid"):
        return str(pr["headRefOid"])
    raise GateError("PR JSON is missing head.sha/headRefOid")


def is_review_discoverable(pr: dict[str, Any], *, allow_draft: bool = False) -> bool:
    """Return whether a PR belongs in ordinary Review discovery."""
    if _state(pr) != "open":
        return False
    if _draft(pr) and not allow_draft:
        return False
    return True


def _latest_required_status(combined_status: dict[str, Any]) -> dict[str, Any]:
    statuses = combined_status.get("statuses")
    if not isinstance(statuses, list):
        raise GateError("combined status JSON is missing statuses[]")
    matches = [
        status
        for status in statuses
        if isinstance(status, dict) and status.get("context") == REQUIRED_ORDINARY_CI_CONTEXT
    ]
    if not matches:
        raise GateError(
            f"required ordinary CI status {REQUIRED_ORDINARY_CI_CONTEXT!r} is absent"
        )
    return max(
        matches,
        key=lambda status: str(status.get("updated_at") or status.get("created_at") or ""),
    )


def evaluate_integration_gate(
    pr: dict[str, Any],
    *,
    reviewed_head: str,
    combined_status: dict[str, Any],
) -> dict[str, Any]:
    """Validate that Integration is acting on the exact reviewed, fully certified PR head."""
    if _state(pr) != "open":
        raise GateError(f"PR state is {_state(pr)!r}, expected 'open'")
    if _draft(pr):
        raise GateError("PR is draft; ordinary integration requires review-ready state")

    current_head = _head_sha(pr)
    if current_head != reviewed_head:
        raise GateError(
            f"PR head moved: reviewed {reviewed_head}, current {current_head}; re-review is required"
        )

    status_sha = str(combined_status.get("sha", ""))
    if status_sha != reviewed_head:
        raise GateError(
            f"required CI evidence is for {status_sha or '<missing>'}, not reviewed head {reviewed_head}"
        )

    required = _latest_required_status(combined_status)
    state = str(required.get("state", "")).lower()
    if state != "success":
        raise GateError(
            f"required ordinary CI status is {state or '<missing>'}, expected 'success'"
        )

    return {
        "ok": True,
        "current_head": current_head,
        "reviewed_head": reviewed_head,
        "certified_sha": status_sha,
        "required_status_context": REQUIRED_ORDINARY_CI_CONTEXT,
        "required_status_state": state,
        "target_url": required.get("target_url"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr_gate")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review-ready", help="test ordinary Review discoverability")
    review.add_argument("--pr-json", required=True)
    review.add_argument(
        "--allow-draft",
        action="store_true",
        help="Marco-requested exceptional early review of a draft PR",
    )

    integration = sub.add_parser(
        "integration", help="verify exact reviewed head and required ordinary CI certification"
    )
    integration.add_argument("--pr-json", required=True)
    integration.add_argument("--reviewed-head", required=True)
    integration.add_argument("--status-json", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        pr = _load_json(args.pr_json)
        if args.command == "review-ready":
            result = {
                "discoverable": is_review_discoverable(pr, allow_draft=args.allow_draft),
                "draft": _draft(pr),
                "head_sha": _head_sha(pr),
                "state": _state(pr),
            }
            print(json.dumps(result, sort_keys=True))
            return 0 if result["discoverable"] else 3

        result = evaluate_integration_gate(
            pr,
            reviewed_head=args.reviewed_head,
            combined_status=_load_json(args.status_json),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except GateError as exc:
        print(f"pr_gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
