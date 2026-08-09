"""Cryptographic and restore-fence primitives for frontend authentication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from argon2.low_level import Type

PASSWORD_MIN_CODEPOINTS = 16
PASSWORD_MAX_CODEPOINTS = 1024
SESSION_LIFETIME_SECONDS = 604800
SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_FENCE_RE = re.compile(r"^dish-frontend-restore-fence-v1:([A-Za-z0-9_-]{43})\n?$")


class FrontendSecurityConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Argon2Policy:
    time_cost: int
    memory_cost_kib: int
    parallelism: int
    hash_len: int
    salt_len: int
    min_time_cost: int
    max_time_cost: int
    min_memory_cost_kib: int
    max_memory_cost_kib: int
    min_parallelism: int
    max_parallelism: int

    def __post_init__(self) -> None:
        values = (
            self.time_cost,
            self.memory_cost_kib,
            self.parallelism,
            self.hash_len,
            self.salt_len,
            self.min_time_cost,
            self.max_time_cost,
            self.min_memory_cost_kib,
            self.max_memory_cost_kib,
            self.min_parallelism,
            self.max_parallelism,
        )
        if any(not isinstance(value, int) or value <= 0 for value in values):
            raise FrontendSecurityConfigurationError("Argon2 policy values must be positive integers")
        if not self.min_time_cost <= self.time_cost <= self.max_time_cost:
            raise FrontendSecurityConfigurationError("Argon2 time cost is outside the configured approved range")
        if not self.min_memory_cost_kib <= self.memory_cost_kib <= self.max_memory_cost_kib:
            raise FrontendSecurityConfigurationError("Argon2 memory cost is outside the configured approved range")
        if not self.min_parallelism <= self.parallelism <= self.max_parallelism:
            raise FrontendSecurityConfigurationError("Argon2 parallelism is outside the configured approved range")
        if self.hash_len < 16 or self.salt_len < 16:
            raise FrontendSecurityConfigurationError("Argon2 hash and salt lengths must be at least 16 bytes")

    def hasher(self) -> PasswordHasher:
        return PasswordHasher(
            time_cost=self.time_cost,
            memory_cost=self.memory_cost_kib,
            parallelism=self.parallelism,
            hash_len=self.hash_len,
            salt_len=self.salt_len,
            type=Type.ID,
        )

    def validate_verifier(self, verifier: str) -> None:
        try:
            params = extract_parameters(verifier)
        except InvalidHashError as exc:
            raise FrontendSecurityConfigurationError("frontend password verifier is not a valid Argon2 hash") from exc
        if params.type is not Type.ID:
            raise FrontendSecurityConfigurationError("frontend password verifier must use Argon2id")
        if not self.min_time_cost <= params.time_cost <= self.max_time_cost:
            raise FrontendSecurityConfigurationError("stored Argon2 time cost is outside the approved range")
        if not self.min_memory_cost_kib <= params.memory_cost <= self.max_memory_cost_kib:
            raise FrontendSecurityConfigurationError("stored Argon2 memory cost is outside the approved range")
        if not self.min_parallelism <= params.parallelism <= self.max_parallelism:
            raise FrontendSecurityConfigurationError("stored Argon2 parallelism is outside the approved range")
        if params.hash_len != self.hash_len:
            raise FrontendSecurityConfigurationError("stored Argon2 hash length does not match the approved policy")
        if params.salt_len != self.salt_len:
            raise FrontendSecurityConfigurationError("stored Argon2 salt length does not match the approved policy")


def password_is_within_policy(password: str) -> bool:
    return isinstance(password, str) and PASSWORD_MIN_CODEPOINTS <= len(password) <= PASSWORD_MAX_CODEPOINTS


def require_provisionable_password(password: str, *, forbidden_secrets: tuple[str, ...] = ()) -> str:
    if not password_is_within_policy(password):
        raise FrontendSecurityConfigurationError(
            f"frontend password must contain {PASSWORD_MIN_CODEPOINTS}-{PASSWORD_MAX_CODEPOINTS} Unicode code points"
        )
    if any(hmac.compare_digest(password, secret) for secret in forbidden_secrets if secret):
        raise FrontendSecurityConfigurationError("frontend password must differ from every configured service/security secret")
    return password


def verify_password(verifier: str, password: str, policy: Argon2Policy) -> bool:
    policy.validate_verifier(verifier)
    try:
        return bool(policy.hasher().verify(verifier, password))
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHashError) as exc:
        raise FrontendSecurityConfigurationError("frontend password verification could not be established") from exc


def encode_token(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_session_token() -> str:
    return encode_token(secrets.token_bytes(SESSION_TOKEN_BYTES))


def valid_session_token(token: str) -> bool:
    return isinstance(token, str) and _SESSION_RE.fullmatch(token) is not None


def keyed_digest(secret: bytes, purpose: bytes, value: str) -> bytes:
    return hmac.new(secret, purpose + b"\0" + value.encode("utf-8"), hashlib.sha256).digest()


def token_verifier(secret: bytes, token: str) -> bytes:
    return keyed_digest(secret, b"session", token)


def csrf_proof(secret: bytes, token: str) -> str:
    return encode_token(hmac.new(secret, b"csrf\0" + token.encode("ascii"), hashlib.sha256).digest())


def peer_digest(secret: bytes, peer: str) -> bytes:
    return keyed_digest(secret, b"peer", peer)


def restore_fence_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def read_restore_fence(path: Path) -> str:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise FrontendSecurityConfigurationError("frontend restore fence permissions are unsafe")
        payload = os.read(descriptor, 128)
        if os.read(descriptor, 1):
            raise FrontendSecurityConfigurationError("frontend restore fence is malformed")
        value = payload.decode("ascii")
    except FrontendSecurityConfigurationError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise FrontendSecurityConfigurationError("frontend restore fence is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    match = _FENCE_RE.fullmatch(value)
    if match is None:
        raise FrontendSecurityConfigurationError("frontend restore fence is malformed")
    return match.group(1)


def _fsync_parent(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_fence_file(path: Path, payload: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def create_restore_fence(path: Path, *, replace: bool = False) -> str:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = new_session_token()
    payload = f"dish-frontend-restore-fence-v1:{value}\n"
    temporary: Path | None = None
    try:
        if replace:
            temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
            _write_new_fence_file(temporary, payload)
            os.replace(temporary, path)
            temporary = None
        else:
            _write_new_fence_file(path, payload)
        _fsync_parent(path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise FrontendSecurityConfigurationError("could not write frontend restore fence") from exc
    return value
