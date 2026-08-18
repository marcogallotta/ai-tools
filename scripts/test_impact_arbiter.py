#!/usr/bin/env python3
"""Union independently produced test-obligation envelopes without narrowing them."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Mapping

FORMAT = "dish-test-obligations-v1"
UNION_FORMAT = "dish-test-obligation-union-v1"
BOUNDARIES = {
    "python-control-plane",
    "frontend-static",
    "native-postgresql",
    "browser-acceptance",
}
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class ArbiterError(ValueError):
    """An envelope cannot participate in a trustworthy non-narrowing union."""


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _strings(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ArbiterError(f"{field} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ArbiterError(f"{field} must not contain duplicates")
    return tuple(value)


def validate_envelope(raw: object, *, expected_provenance: str) -> dict[str, object]:
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise ArbiterError(f"envelope format must be {FORMAT}")
    if raw.get("provenance") != expected_provenance:
        raise ArbiterError(f"envelope provenance must be {expected_provenance}")
    engine_identity = raw.get("engine_identity")
    if not isinstance(engine_identity, str) or not _DIGEST_RE.fullmatch(engine_identity):
        raise ArbiterError("engine_identity must be a SHA-256 digest")
    paths = _strings(raw.get("changed_paths"), field="changed_paths")
    obligations = raw.get("obligations")
    if not isinstance(obligations, list):
        raise ArbiterError("obligations must be an array")
    normalized: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(obligations):
        if not isinstance(item, dict):
            raise ArbiterError(f"obligations[{index}] must be an object")
        path = item.get("path")
        key = item.get("key")
        guarantee = item.get("guarantee")
        boundary = item.get("execution_boundary")
        fallback = item.get("fallback_target")
        if not all(isinstance(value, str) and value for value in (path, key, guarantee, boundary, fallback)):
            raise ArbiterError(f"obligations[{index}] has an empty semantic field")
        if path not in paths:
            raise ArbiterError(f"obligations[{index}] path is outside changed_paths")
        if boundary not in BOUNDARIES:
            raise ArbiterError(f"obligations[{index}] has unknown execution boundary {boundary}")
        preferred = _strings(item.get("preferred_targets"), field=f"obligations[{index}].preferred_targets")
        identity = (path, key, expected_provenance)
        if identity in seen:
            raise ArbiterError(f"duplicate obligation: {path} {key} {expected_provenance}")
        seen.add(identity)
        normalized.append({
            "path": path,
            "key": key,
            "guarantee": guarantee,
            "execution_boundary": boundary,
            "preferred_targets": list(preferred),
            "fallback_target": fallback,
            "provenance": expected_provenance,
        })
    normalized.sort(key=lambda item: (str(item["path"]), str(item["key"])))
    return {
        "format": FORMAT,
        "provenance": expected_provenance,
        "engine_identity": engine_identity,
        "changed_paths": list(paths),
        "obligations": normalized,
    }


def union_envelopes(base: object, candidate: object) -> dict[str, object]:
    left = validate_envelope(base, expected_provenance="base")
    right = validate_envelope(candidate, expected_provenance="candidate")
    if left["changed_paths"] != right["changed_paths"]:
        raise ArbiterError("base and candidate envelopes must cover the same changed paths")
    obligations = [*left["obligations"], *right["obligations"]]  # type: ignore[list-item]
    obligations.sort(
        key=lambda item: (str(item["path"]), str(item["key"]), str(item["provenance"]))
    )
    semantic_keys = sorted({
        (str(item["path"]), str(item["key"])) for item in obligations
    })
    return {
        "format": UNION_FORMAT,
        "base_engine_identity": left["engine_identity"],
        "candidate_engine_identity": right["engine_identity"],
        "base_obligation_digest": _digest(left["obligations"]),
        "candidate_obligation_digest": _digest(right["obligations"]),
        "union_digest": _digest(obligations),
        "semantic_keys": [list(value) for value in semantic_keys],
        "obligations": obligations,
    }


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArbiterError(f"cannot read envelope {path}: {exc}") from exc


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="test_impact_arbiter.py")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = union_envelopes(_read(args.base), _read(args.candidate))
    except ArbiterError as exc:
        print(f"test-impact-arbiter: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
