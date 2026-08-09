"""Frontend password/session application boundary."""
from __future__ import annotations

import hmac
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from dish_pg.frontend_security_repository import FrontendSecurityRepository
from .frontend_security import (
    Argon2Policy,
    FrontendSecurityConfigurationError,
    SESSION_LIFETIME_SECONDS,
    csrf_proof,
    peer_digest,
    read_restore_fence,
    restore_fence_digest,
    token_verifier,
    valid_session_token,
    verify_password,
    new_session_token,
)

_LIMIT_WINDOW = timedelta(minutes=15)
_PEER_FAILURE_LIMIT = 5
_GLOBAL_FAILURE_LIMIT = 30


class FrontendAuthFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class FrontendPrincipal:
    session_id: str
    expires_at: datetime
    security_generation: int


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str
    principal: FrontendPrincipal


@dataclass(frozen=True, slots=True)
class SessionBootstrap:
    principal: FrontendPrincipal
    csrf_proof: str
    remaining_seconds: int


class FrontendAuthService:
    def __init__(
        self,
        factory: sessionmaker,
        *,
        restore_fence_path,
        session_secret: bytes,
        csrf_secret: bytes,
        peer_secret: bytes,
        argon2_policy: Argon2Policy,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.factory = factory
        self.restore_fence_path = restore_fence_path
        self.session_secret = session_secret
        self.csrf_secret = csrf_secret
        self.peer_secret = peer_secret
        self.argon2_policy = argon2_policy
        self.now = now or (lambda: datetime.now(timezone.utc))

    def startup_check(self) -> None:
        try:
            fence_sha = self._current_fence_sha()
        except FrontendAuthFailure as exc:
            raise FrontendSecurityConfigurationError("frontend restore fence is unavailable") from exc
        try:
            with self.factory.begin() as session:
                state = FrontendSecurityRepository(session).state()
                if state is None:
                    raise FrontendSecurityConfigurationError("frontend password has not been provisioned")
                self.argon2_policy.validate_verifier(state.password_verifier)
                if state.restore_fence_sha256 != fence_sha:
                    raise FrontendSecurityConfigurationError("frontend restore fence does not match PostgreSQL security state")
        except SQLAlchemyError as exc:
            raise FrontendSecurityConfigurationError("frontend security persistence is unavailable") from exc

    def login(self, *, password: str, peer: str, presented_token: str | None = None) -> LoginResult:
        now = self._utc_now()
        peer_hash = peer_digest(self.peer_secret, peer)
        fence_sha = self._current_fence_sha()
        throttled_retry: int | None = None
        invalid_password = False
        token: str | None = None
        principal: FrontendPrincipal | None = None
        try:
            # The singleton security-state row is the login/rotation serialization gate.
            # Holding it through Argon2 makes the durable limiter decision strict under
            # concurrent attempts and orders password/security rotation against session
            # creation without adding a second authentication authority.
            with self.factory.begin() as session:
                repo = FrontendSecurityRepository(session)
                state = repo.state(for_update=True)
                if state is None or state.restore_fence_sha256 != fence_sha:
                    raise FrontendAuthFailure("service_unavailable", "Login is temporarily unavailable.")
                self.argon2_policy.validate_verifier(state.password_verifier)
                retry = self._retry_after(repo, peer_hash, now)
                if retry is not None:
                    repo.add_login_event(peer=peer_hash, outcome="throttled", now=now, retry_after_seconds=retry)
                    repo.add_audit(
                        event_type="login_throttled", now=now,
                        generation=state.security_generation, peer=peer_hash,
                    )
                    throttled_retry = retry
                else:
                    matched = verify_password(state.password_verifier, password, self.argon2_policy)
                    if not matched:
                        peer_failures, global_failures = repo.failure_counts(
                            peer=peer_hash, since=now - _LIMIT_WINDOW
                        )
                        repo.add_login_event(
                            peer=peer_hash, outcome="failure", now=now,
                            peer_blocked_until=(
                                now + _LIMIT_WINDOW if peer_failures + 1 >= _PEER_FAILURE_LIMIT else None
                            ),
                            global_blocked_until=(
                                now + _LIMIT_WINDOW if global_failures + 1 >= _GLOBAL_FAILURE_LIMIT else None
                            ),
                        )
                        repo.add_audit(
                            event_type="login_failure", now=now,
                            generation=state.security_generation, peer=peer_hash,
                        )
                        invalid_password = True
                    else:
                        token = new_session_token()
                        if presented_token and valid_session_token(presented_token):
                            old = repo.session_by_verifier(
                                token_verifier(self.session_secret, presented_token),
                                for_update=True,
                            )
                            if old is not None:
                                repo.revoke(old, now=now)
                        expires_at = now + timedelta(seconds=SESSION_LIFETIME_SECONDS)
                        row = repo.create_session(
                            verifier=token_verifier(self.session_secret, token),
                            generation=state.security_generation,
                            restore_fence_sha256=state.restore_fence_sha256,
                            peer=peer_hash,
                            issued_at=now,
                            expires_at=expires_at,
                        )
                        repo.add_login_event(peer=peer_hash, outcome="success", now=now)
                        repo.add_audit(
                            event_type="login_success", now=now, generation=state.security_generation,
                            session_id=row.session_id, peer=peer_hash,
                        )
                        repo.cleanup(now=now)
                        principal = FrontendPrincipal(
                            str(row.session_id), self._as_utc(expires_at), state.security_generation,
                        )
        except FrontendAuthFailure:
            raise
        except (SQLAlchemyError, FrontendSecurityConfigurationError) as exc:
            raise FrontendAuthFailure("service_unavailable", "Login is temporarily unavailable.") from exc
        if throttled_retry is not None:
            raise FrontendAuthFailure(
                "login_throttled", "Login is temporarily delayed.",
                retry_after_seconds=throttled_retry,
            )
        if invalid_password:
            raise FrontendAuthFailure("login_invalid", "The shared password was not accepted.")
        assert token is not None and principal is not None
        return LoginResult(token=token, principal=principal)

    def bootstrap(self, token: str) -> SessionBootstrap:
        principal = self.validate(token, record_expiry=True)
        remaining = max(0, min(SESSION_LIFETIME_SECONDS, int((principal.expires_at - self._utc_now()).total_seconds())))
        return SessionBootstrap(
            principal=principal,
            csrf_proof=csrf_proof(self.csrf_secret, token),
            remaining_seconds=remaining,
        )

    def validate(self, token: str, *, record_expiry: bool = False) -> FrontendPrincipal:
        if not valid_session_token(token):
            raise FrontendAuthFailure("auth_required", "Authentication is required.")
        now = self._utc_now()
        fence_sha = self._current_fence_sha()
        try:
            with self.factory.begin() as session:
                repo = FrontendSecurityRepository(session)
                state = repo.state()
                row = repo.session_by_verifier(token_verifier(self.session_secret, token), for_update=record_expiry)
                if row is None or state is None:
                    raise FrontendAuthFailure("auth_required", "Authentication is required.")
                expires_at = self._as_utc(row.expires_at)
                if expires_at <= now:
                    if record_expiry and row.revoked_at is None:
                        repo.revoke(row, now=now)
                        repo.add_audit(event_type="session_expired", now=now, generation=row.security_generation, session_id=row.session_id)
                    raise FrontendAuthFailure("session_expired", "The session has expired.")
                if row.revoked_at is not None:
                    raise FrontendAuthFailure("session_revoked", "The session is no longer valid.")
                if (
                    row.security_generation != state.security_generation
                    or row.restore_fence_sha256 != fence_sha
                    or state.restore_fence_sha256 != fence_sha
                ):
                    raise FrontendAuthFailure("session_revoked", "The session is no longer valid.")
                return FrontendPrincipal(str(row.session_id), expires_at, row.security_generation)
        except FrontendAuthFailure:
            raise
        except (SQLAlchemyError, FrontendSecurityConfigurationError) as exc:
            raise FrontendAuthFailure("session_unavailable", "Session validation is temporarily unavailable.") from exc

    def logout(self, token: str, *, csrf: str) -> None:
        if not valid_session_token(token):
            raise FrontendAuthFailure("auth_required", "Authentication is required.")
        expected = csrf_proof(self.csrf_secret, token)
        if not hmac.compare_digest(csrf, expected):
            raise FrontendAuthFailure("csrf_rejected", "Logout verification was rejected.")
        now = self._utc_now()
        try:
            with self.factory.begin() as session:
                repo = FrontendSecurityRepository(session)
                row = repo.session_by_verifier(token_verifier(self.session_secret, token), for_update=True)
                state = repo.state()
                if row is None or state is None:
                    raise FrontendAuthFailure("auth_required", "Authentication is required.")
                changed = repo.revoke(row, now=now)
                if changed:
                    repo.add_audit(event_type="logout", now=now, generation=row.security_generation, session_id=row.session_id)
        except FrontendAuthFailure:
            raise
        except SQLAlchemyError as exc:
            raise FrontendAuthFailure("logout_unavailable", "Logout could not be confirmed.") from exc

    def _current_fence_sha(self) -> str:
        try:
            return restore_fence_digest(read_restore_fence(self.restore_fence_path))
        except FrontendSecurityConfigurationError as exc:
            raise FrontendAuthFailure("service_unavailable", "Frontend security state is unavailable.") from exc

    @staticmethod
    def _retry_after(repo: FrontendSecurityRepository, peer: bytes, now: datetime) -> int | None:
        deadline = repo.active_block_deadline(peer=peer, now=now)
        if deadline is None:
            return None
        remaining = (FrontendAuthService._as_utc(deadline) - now).total_seconds()
        return max(1, min(900, math.ceil(remaining)))

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    def _utc_now(self) -> datetime:
        return self._as_utc(self.now())
