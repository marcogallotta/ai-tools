"""Stable causal identity shared by full-regression and PR CI recovery."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

SCHEMA = "dish-ci-causal-fingerprint-v1"
_FINGERPRINT_RE = re.compile(r"^ci-cause-v1:[0-9a-f]{32}$")
_SPACE_RE = re.compile(r"\s+")


class FingerprintError(ValueError):
    """Raised when evidence is too weak or malformed for causal dedupe."""


def _normalize(value: str, label: str) -> str:
    normalized = _SPACE_RE.sub(" ", str(value).strip().lower())
    if not normalized:
        raise FingerprintError(f"{label} is required for causal fingerprinting")
    return normalized


def causal_identity(
    *, owner_surface: str, failure_surface: str, invariant: str, signature: str
) -> dict[str, str]:
    """Return the normalized, occurrence-independent cause used for dedupe.

    Run IDs and commit SHAs are deliberately not accepted here. They belong on
    occurrence evidence, so a continuing defect survives reruns and main moves.
    """
    return {
        "schema": SCHEMA,
        "owner_surface": _normalize(owner_surface, "owner_surface"),
        "failure_surface": _normalize(failure_surface, "failure_surface"),
        "invariant": _normalize(invariant, "invariant"),
        "signature": _normalize(signature, "signature"),
    }


def causal_fingerprint(
    *, owner_surface: str, failure_surface: str, invariant: str, signature: str
) -> tuple[str, dict[str, str]]:
    identity = causal_identity(
        owner_surface=owner_surface,
        failure_surface=failure_surface,
        invariant=invariant,
        signature=signature,
    )
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"ci-cause-v1:{hashlib.sha256(encoded).hexdigest()[:32]}", identity


def validate_fingerprint(value: str) -> str:
    value = str(value).strip().lower()
    if not _FINGERPRINT_RE.fullmatch(value):
        raise FingerprintError("causal fingerprint must use ci-cause-v1:<32 lowercase hex>")
    return value


def fingerprint_from_mapping(value: Mapping[str, object]) -> tuple[str, dict[str, str]]:
    return causal_fingerprint(
        owner_surface=str(value.get("owner_surface") or ""),
        failure_surface=str(value.get("failure_surface") or ""),
        invariant=str(value.get("invariant") or ""),
        signature=str(value.get("signature") or ""),
    )
