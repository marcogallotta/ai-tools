"""Stable causal identity shared by full-regression and PR CI recovery."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping

SCHEMA = "dish-ci-causal-fingerprint-v1"
_FINGERPRINT_RE = re.compile(r"^ci-cause-v1:[0-9a-f]{32}$")
_SPACE_RE = re.compile(r"\s+")
_SHA_RE = re.compile(r"\b[0-9a-f]{40,64}\b", re.I)
_RUN_RE = re.compile(r"\b(?:run|job|attempt|request)[ _-]?(?:id)?[=:# /-]*\d+\b", re.I)
_VOLATILE_TIME_RE = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|sec|seconds|minutes?)\b", re.I)

WEAK_FAILURE_KINDS = frozenset({"command_failed", "missing_result", "environment_unavailable"})


class FingerprintError(ValueError):
    """Raised when evidence is too weak or malformed for causal dedupe."""


def _normalize(value: str, label: str) -> str:
    normalized = _SPACE_RE.sub(" ", str(value).strip().lower())
    if not normalized:
        raise FingerprintError(f"{label} is required for causal fingerprinting")
    return normalized


def normalize_signature(value: str) -> str:
    """Remove occurrence-only tokens while retaining material failure detail."""
    normalized = _normalize(value, "signature")
    normalized = _SHA_RE.sub("<sha>", normalized)
    normalized = _RUN_RE.sub("<run>", normalized)
    normalized = _VOLATILE_TIME_RE.sub("<duration>", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


@dataclass(frozen=True)
class CausalIdentity:
    owner_surface: str
    failure_surface: str
    invariant: str
    signature: str

    @classmethod
    def build(
        cls, *, owner_surface: str, failure_surface: str, invariant: str, signature: str
    ) -> "CausalIdentity":
        return cls(
            owner_surface=_normalize(owner_surface, "owner_surface"),
            failure_surface=_normalize(failure_surface, "failure_surface"),
            invariant=_normalize(invariant, "invariant"),
            signature=normalize_signature(signature),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CausalIdentity":
        if str(value.get("schema") or "") != SCHEMA:
            raise FingerprintError(f"causal identity schema must be {SCHEMA}")
        return cls.build(
            owner_surface=str(value.get("owner_surface") or ""),
            failure_surface=str(value.get("failure_surface") or ""),
            invariant=str(value.get("invariant") or ""),
            signature=str(value.get("signature") or ""),
        )

    def json(self) -> dict[str, str]:
        return {
            "schema": SCHEMA,
            "owner_surface": self.owner_surface,
            "failure_surface": self.failure_surface,
            "invariant": self.invariant,
            "signature": self.signature,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.json(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"ci-cause-v1:{hashlib.sha256(encoded).hexdigest()[:32]}"


def cause_for_failure(
    *, owner_surface: str, failure_surface: str, invariant: str,
    failure_kind: str, detail: str | None,
) -> CausalIdentity | None:
    """Return a strong cause, or None when only occurrence-level evidence exists."""
    kind = _normalize(failure_kind, "failure_kind")
    stable_detail = str(detail or "").strip()
    if kind in WEAK_FAILURE_KINDS or not stable_detail:
        return None
    signature = f"{kind}: {stable_detail}"
    return CausalIdentity.build(
        owner_surface=owner_surface,
        failure_surface=failure_surface,
        invariant=invariant,
        signature=signature,
    )


def validate_cause(*, fingerprint: str, identity: Mapping[str, object]) -> CausalIdentity:
    cause = CausalIdentity.from_mapping(identity)
    supplied = validate_fingerprint(fingerprint)
    if supplied != cause.fingerprint:
        raise FingerprintError("causal fingerprint does not match normalized causal identity")
    return cause


def validate_fingerprint(value: str) -> str:
    value = str(value).strip().lower()
    if not _FINGERPRINT_RE.fullmatch(value):
        raise FingerprintError("causal fingerprint must use ci-cause-v1:<32 lowercase hex>")
    return value


def causal_fingerprint(
    *, owner_surface: str, failure_surface: str, invariant: str, signature: str
) -> tuple[str, dict[str, str]]:
    """Compatibility helper for producers that already have a stable signature."""
    cause = CausalIdentity.build(
        owner_surface=owner_surface,
        failure_surface=failure_surface,
        invariant=invariant,
        signature=signature,
    )
    return cause.fingerprint, cause.json()


def fingerprint_from_mapping(value: Mapping[str, object]) -> tuple[str, dict[str, str]]:
    cause = CausalIdentity.from_mapping(value)
    return cause.fingerprint, cause.json()
