"""Shared exact-head admission predicate for local-first code quality."""
from __future__ import annotations

from dataclasses import dataclass
import json
import tomllib
from typing import Any, Iterable

from code_quality_common import GateError, JSON_RE, MARKER_RE, SCHEMA, _digest, _sha


@dataclass(frozen=True)
class Admission:
    allowed: bool
    enabled: bool
    reason: str
    result: dict[str, Any] | None = None


def _sha_from(pr: dict[str, Any], side: str) -> str:
    value = pr.get(side)
    if isinstance(value, dict) and value.get("sha"):
        return str(value["sha"])
    key = "headRefOid" if side == "head" else "baseRefOid"
    return str(pr.get(key) or "")


def github_admission(github: Any, pr: dict[str, Any], comments: Iterable[dict[str, Any]]) -> Admission:
    """Read both exact policies and apply the shared predicate to live PR state."""
    reader = getattr(github, "get_file_bytes", None)
    if not callable(reader):
        # Compatibility for non-network test doubles. Production GitHubREST has
        # the capability and therefore never takes this path.
        return Admission(True, False, "code-quality policy reader is unavailable")
    head = _sha_from(pr, "head")
    base = _sha_from(pr, "base")
    try:
        number = int(pr.get("number"))
    except (TypeError, ValueError) as exc:
        raise GateError("PR number is missing") from exc
    return exact_head_admission(
        comments=comments,
        head=head,
        target_base=base,
        pr_number=number,
        base_policy=reader("ci/code-quality.toml", base),
        head_policy=reader("ci/code-quality.toml", head),
    )


def policy_enabled(raw: bytes | str | None, *, missing: bool = False) -> bool:
    if raw is None:
        return missing
    data = raw.encode() if isinstance(raw, str) else raw
    try:
        value = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GateError(f"code-quality policy is invalid: {exc}") from exc
    if int(value.get("version", 0)) != 1 or not isinstance(value.get("enabled"), bool):
        raise GateError("code-quality policy must have version=1 and boolean enabled")
    return bool(value["enabled"])


def exact_head_admission(
    *,
    comments: Iterable[dict[str, Any]],
    head: str,
    target_base: str,
    pr_number: int,
    base_policy: bytes | str | None,
    head_policy: bytes | str | None,
) -> Admission:
    """Apply the monotonic policy and require one acceptable author result when enabled."""
    enabled = policy_enabled(base_policy) or policy_enabled(head_policy)
    if not enabled:
        return Admission(True, False, "code-quality enforcement is disabled on base and head")

    valid: list[dict[str, Any]] = []
    errors: list[str] = []
    marker = f"dish-code-quality-result:v1 head={head}"
    for comment in comments:
        body = str(comment.get("body") or "")
        if marker not in body:
            continue
        try:
            markers = MARKER_RE.findall(body)
            blocks = JSON_RE.findall(body)
            if len(markers) != 1 or len(blocks) != 1:
                raise GateError("code-quality comment must contain one marker and one JSON result")
            marker_head, marker_digest = markers[0]
            result = json.loads(blocks[0])
            expected_digest = _digest({k: v for k, v in result.items() if k != "result_digest"})
            if marker_head != _sha(head, "head") or result.get("head_sha") != head:
                raise GateError("code-quality comment head is stale")
            if result.get("schema") != SCHEMA:
                raise GateError("code-quality result schema is invalid")
            if result.get("target_base_sha") != target_base or result.get("pr_number") != pr_number:
                raise GateError("code-quality result PR/base identity is invalid")
            if result.get("result_digest") != expected_digest or marker_digest != expected_digest:
                raise GateError("code-quality result digest mismatch")
            valid.append(result)
        except (GateError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
    if len(valid) != 1:
        detail = f" ({'; '.join(errors)})" if errors else ""
        return Admission(
            False,
            True,
            f"expected exactly one valid local code-quality result for exact head {head}; found {len(valid)}{detail}",
        )
    result = valid[0]
    if result.get("outcome") not in {"PASS", "BOOTSTRAP"}:
        return Admission(False, True, f"local code-quality outcome is {result.get('outcome')!r}", result)
    if result.get("effective_enabled") is not True:
        return Admission(False, True, "local code-quality result does not prove effective enforcement", result)
    return Admission(True, True, "exact-head local code-quality result accepted", result)
