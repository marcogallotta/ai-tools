#!/usr/bin/env python3
"""Fail-closed admission for trusted code-quality verification attestations."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "dish-code-quality-attestation-v1"
WORKFLOW_PATH = ".github/workflows/code-quality.yml"
TRUSTED_EVENT = "issue_comment"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class AttestationError(ValueError):
    """The supplied Actions evidence is not authoritative code-quality verification."""


def artifact_name(
    *, pr_number: int, head_sha: str, result_digest: str, run_id: int, run_attempt: int
) -> str:
    _validate_identity(pr_number=pr_number, head_sha=head_sha, result_digest=result_digest)
    if run_id < 1 or run_attempt < 1:
        raise AttestationError("workflow run id and attempt must be positive")
    return (
        f"{SCHEMA}-pr{pr_number}-{head_sha}-{result_digest}"
        f"-run{run_id}-attempt{run_attempt}"
    )


def _validate_identity(*, pr_number: int, head_sha: str, result_digest: str) -> None:
    if pr_number < 1:
        raise AttestationError("PR number must be positive")
    if not _SHA_RE.fullmatch(head_sha):
        raise AttestationError("head SHA must be 40 lowercase hex characters")
    if not _DIGEST_RE.fullmatch(result_digest):
        raise AttestationError("result digest must be 64 lowercase hex characters")


def _run_int(run: dict[str, Any], field: str) -> int:
    try:
        value = int(run.get(field))
    except (TypeError, ValueError) as exc:
        raise AttestationError(f"workflow run is missing numeric {field}") from exc
    if value < 1:
        raise AttestationError(f"workflow run {field} must be positive")
    return value


def verify_attestation(
    workflow_run: dict[str, Any],
    artifacts_payload: dict[str, Any],
    *,
    repository: str,
    default_branch: str,
    pr_number: int,
    head_sha: str,
    result_digest: str,
) -> dict[str, Any]:
    """Verify that trusted default-branch code attested the exact PR result."""
    _validate_identity(pr_number=pr_number, head_sha=head_sha, result_digest=result_digest)
    run_id = _run_int(workflow_run, "id")
    run_attempt = _run_int(workflow_run, "run_attempt")
    if workflow_run.get("path") != WORKFLOW_PATH:
        raise AttestationError("workflow run path is not the code-quality verifier")
    if workflow_run.get("event") != TRUSTED_EVENT:
        raise AttestationError("workflow run event is not trusted issue_comment verification")
    if workflow_run.get("head_branch") != default_branch:
        raise AttestationError("workflow run did not execute from the trusted default branch")
    if str(workflow_run.get("status") or "").lower() != "completed":
        raise AttestationError("workflow run is not completed")
    if str(workflow_run.get("conclusion") or "").lower() != "success":
        raise AttestationError("workflow run did not conclude success")
    repo = workflow_run.get("repository")
    if isinstance(repo, dict) and repo.get("full_name") not in {None, repository}:
        raise AttestationError("workflow run repository does not match expected repository")

    values = artifacts_payload.get("artifacts")
    if not isinstance(values, list):
        raise AttestationError("artifacts payload is missing artifacts[]")
    expected_name = artifact_name(
        pr_number=pr_number,
        head_sha=head_sha,
        result_digest=result_digest,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    matches: list[dict[str, Any]] = []
    for artifact in values:
        if not isinstance(artifact, dict) or artifact.get("name") != expected_name:
            continue
        if bool(artifact.get("expired")):
            continue
        owner = artifact.get("workflow_run")
        if isinstance(owner, dict):
            try:
                if int(owner.get("id")) != run_id:
                    continue
            except (TypeError, ValueError):
                continue
        matches.append(artifact)
    if len(matches) != 1:
        raise AttestationError(
            f"expected exactly one unexpired exact-head attestation artifact, found {len(matches)}"
        )
    return {
        "authoritative": True,
        "schema": SCHEMA,
        "repository": repository,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "result_digest": result_digest,
        "workflow_run_id": run_id,
        "workflow_run_attempt": run_attempt,
        "artifact_name": expected_name,
    }


def _load(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AttestationError(f"could not read JSON {path!r}: {exc}") from exc
    if not isinstance(value, dict):
        raise AttestationError(f"expected JSON object in {path!r}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code_quality_attestation")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="verify trusted exact-head code-quality evidence")
    verify.add_argument("--run-json", required=True)
    verify.add_argument("--artifacts-json", required=True)
    verify.add_argument("--repository", required=True)
    verify.add_argument("--default-branch", required=True)
    verify.add_argument("--pr-number", type=int, required=True)
    verify.add_argument("--head", required=True)
    verify.add_argument("--result-digest", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_attestation(
            _load(args.run_json),
            _load(args.artifacts_json),
            repository=args.repository,
            default_branch=args.default_branch,
            pr_number=args.pr_number,
            head_sha=args.head,
            result_digest=args.result_digest,
        )
    except AttestationError as exc:
        print(f"code_quality_attestation: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
