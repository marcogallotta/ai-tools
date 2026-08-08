"""Opaque browser identities and retry-safe stateless continuation cursors.

Route identities are one-way HMAC labels over internal UUIDs. Cursor packaging
uses a random nonce, a domain-separated HMAC-derived mask, and a separate HMAC
tag. The mask exists only to meet the frontend contract's opacity requirement:
cursor contents are not credentials and this module is not an authorization
boundary.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID

_ROUTE_RE = re.compile(r"r1([st])-([A-Za-z0-9_-]{27})")
_CURSOR_RE = re.compile(r"c1\.([A-Za-z0-9_-]{40,4096})")
_MIN_SECRET_BYTES = 32
_NONCE_BYTES = 16
_TAG_BYTES = 16
_ROUTE_DIGEST_BYTES = 20
MAX_CURSOR_LENGTH = 4096


class CursorInvalid(ValueError):
    """The cursor is malformed, tampered, or scoped to the wrong environment."""


class CursorStale(ValueError):
    """The cursor is structurally valid but no longer usable."""


def _require_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) < _MIN_SECRET_BYTES:
        raise ValueError("frontend token secret must contain at least 32 bytes")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # binascii/Error variants are implementation details here.
        raise CursorInvalid("cursor encoding is invalid") from exc


def route_identity(*, secret: bytes, environment: str, kind: str, object_id: UUID) -> str:
    """Return a deterministic, non-raw route identity for a task or section."""

    _require_secret(secret)
    if kind not in {"task", "section"}:
        raise ValueError("route identity kind must be task or section")
    if not environment or len(environment) > 64:
        raise ValueError("frontend environment must be 1..64 characters")
    tag = "t" if kind == "task" else "s"
    material = b"\0".join(
        (
            b"dish-frontend-route-v1",
            environment.encode("utf-8"),
            kind.encode("ascii"),
            object_id.bytes,
        )
    )
    digest = hmac.new(secret, material, hashlib.sha256).digest()[:_ROUTE_DIGEST_BYTES]
    return f"r1{tag}-{_b64encode(digest)}"


def validate_route_identity(value: str, *, kind: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("route identity is malformed")
    match = _ROUTE_RE.fullmatch(value)
    expected = "t" if kind == "task" else "s" if kind == "section" else None
    if match is None or expected is None or match.group(1) != expected:
        raise ValueError("route identity is malformed or has the wrong type")
    return value


def resolve_route_identity(
    value: str,
    *,
    secret: bytes,
    environment: str,
    kind: str,
    candidates: Iterable[UUID],
) -> UUID | None:
    """Resolve one route identity only across a caller-supplied bounded candidate set."""

    validate_route_identity(value, kind=kind)
    matched: UUID | None = None
    for object_id in candidates:
        candidate = route_identity(
            secret=secret, environment=environment, kind=kind, object_id=object_id
        )
        if hmac.compare_digest(candidate, value):
            if matched is not None:
                raise ValueError("route identity collision within candidate set")
            matched = object_id
    return matched


def opaque_digest(*, secret: bytes, environment: str, purpose: str, payload: Any) -> str:
    """Return a bounded opaque equality identity for canonical JSON presentation input."""

    _require_secret(secret)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    material = b"\0".join(
        (
            b"dish-frontend-digest-v1",
            environment.encode("utf-8"),
            purpose.encode("utf-8"),
            encoded,
        )
    )
    return "d1-" + _b64encode(hmac.new(secret, material, hashlib.sha256).digest()[:20])


def _mask(secret: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    counter = 0
    produced = 0
    while produced < length:
        counter_bytes = counter.to_bytes(4, "big")
        block = hmac.new(
            secret,
            b"dish-frontend-cursor-mask-v1\0" + nonce + counter_bytes,
            hashlib.sha256,
        ).digest()
        blocks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(blocks)[:length]


def seal_cursor(*, secret: bytes, environment: str, payload: dict[str, Any]) -> str:
    """Package cursor state without exposing its internal query-boundary values."""

    _require_secret(secret)
    if not environment or len(environment) > 64:
        raise ValueError("frontend environment must be 1..64 characters")
    body = dict(payload)
    body["environment"] = environment
    plaintext = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    stream = _mask(secret, nonce, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream, strict=True))
    tag = hmac.new(
        secret,
        b"dish-frontend-cursor-tag-v1\0" + nonce + ciphertext,
        hashlib.sha256,
    ).digest()[:_TAG_BYTES]
    token = "c1." + _b64encode(nonce + ciphertext + tag)
    if len(token) > MAX_CURSOR_LENGTH:
        raise ValueError("cursor payload exceeds the browser contract bound")
    return token


def open_cursor(
    value: str,
    *,
    secret: bytes,
    environment: str,
    now: datetime,
) -> dict[str, Any]:
    """Validate, unpack, and expiry-check a stateless cursor."""

    _require_secret(secret)
    if not isinstance(value, str) or len(value) > MAX_CURSOR_LENGTH:
        raise CursorInvalid("cursor is malformed")
    match = _CURSOR_RE.fullmatch(value)
    if match is None:
        raise CursorInvalid("cursor is malformed")
    packed = _b64decode(match.group(1))
    if len(packed) <= _NONCE_BYTES + _TAG_BYTES:
        raise CursorInvalid("cursor is malformed")
    nonce = packed[:_NONCE_BYTES]
    ciphertext = packed[_NONCE_BYTES:-_TAG_BYTES]
    supplied_tag = packed[-_TAG_BYTES:]
    expected_tag = hmac.new(
        secret,
        b"dish-frontend-cursor-tag-v1\0" + nonce + ciphertext,
        hashlib.sha256,
    ).digest()[:_TAG_BYTES]
    if not hmac.compare_digest(supplied_tag, expected_tag):
        raise CursorInvalid("cursor authentication failed")
    stream = _mask(secret, nonce, len(ciphertext))
    plaintext = bytes(a ^ b for a, b in zip(ciphertext, stream, strict=True))
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorInvalid("cursor payload is invalid") from exc
    if not isinstance(payload, dict) or payload.get("environment") != environment:
        raise CursorInvalid("cursor belongs to a different environment")
    expires_raw = payload.get("expires_at")
    if not isinstance(expires_raw, str):
        raise CursorInvalid("cursor expiry is missing")
    try:
        expires_at = datetime.fromisoformat(expires_raw)
    except ValueError as exc:
        raise CursorInvalid("cursor expiry is invalid") from exc
    if expires_at.tzinfo is None:
        raise CursorInvalid("cursor expiry must be timezone-aware")
    now_utc = now.astimezone(timezone.utc)
    if expires_at.astimezone(timezone.utc) <= now_utc:
        raise CursorStale("cursor has expired")
    payload.pop("environment", None)
    return payload
