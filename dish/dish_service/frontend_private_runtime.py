"""Private-listener frontend composition over PostgreSQL security and observation data."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dish_pg.database import DatabaseSettings, create_database_engine, session_factory
from dish_pg.frontend_admin_query import FrontendAdminQuery
from dish_pg.frontend_board_query import BoardReadUnavailable, FrontendBoardQuery
from dish_pg.frontend_detail_query import FrontendDetailQuery
from .frontend_admin import FrontendAdminConfig, FrontendAdminService
from .frontend_auth import FrontendAuthService
from .frontend_board import FrontendBoardConfig, FrontendBoardService
from .frontend_detail import FrontendDetailConfig, FrontendDetailService
from .frontend_security import FrontendSecurityConfigurationError
from .frontend_settings import FrontendRuntimeSettings

_PRIVATE_ENVIRONMENT = "private-postgresql-observation"


@dataclass(frozen=True, slots=True)
class _PostgreSQLDatabaseIdentity:
    database_name: str
    database_oid: int

    def is_same_physical_database(self, other: "_PostgreSQLDatabaseIdentity") -> bool:
        # The intended topology uses different database names. Comparing the
        # server-reported catalog identity is deliberately conservative across
        # servers and fails closed instead of trusting textual DSN differences.
        return (self.database_name, self.database_oid) == (
            other.database_name, other.database_oid
        )


class FrontendDataReadsDisabled(RuntimeError):
    """Raised when authenticated frontend data reads have not been activated."""


class FrontendPrivateRuntime:
    """Own frontend-only PostgreSQL connections and immutable static assets."""

    def __init__(self, settings: FrontendRuntimeSettings) -> None:
        if not settings.enabled:
            raise ValueError("frontend runtime requires enabled settings")
        self.settings = settings
        root = settings.static_root.resolve()  # type: ignore[union-attr]
        if not root.is_dir() or not (root / "index.html").is_file():
            raise FrontendSecurityConfigurationError(f"frontend build directory is incomplete: {root}")
        self.static_root = root
        self.auth_engine = create_database_engine(DatabaseSettings(url=settings.database_url))  # type: ignore[arg-type]
        self.auth_factory = session_factory(self.auth_engine)
        self.observation_engine = None
        self.observation_factory = None
        if settings.postgresql_reads_enabled:
            self.observation_engine = create_database_engine(
                DatabaseSettings(url=settings.observation_database_url)  # type: ignore[arg-type]
            )
            self.observation_factory = session_factory(self.observation_engine)
        self.auth = FrontendAuthService(
            self.auth_factory,
            restore_fence_path=settings.restore_fence_path,
            session_secret=settings.session_secret,  # type: ignore[arg-type]
            csrf_secret=settings.csrf_secret,  # type: ignore[arg-type]
            peer_secret=settings.peer_secret,  # type: ignore[arg-type]
            argon2_policy=settings.argon2_policy,  # type: ignore[arg-type]
        )
        self.board_config = self._board_config(settings)
        self.frontend_openapi = Path(__file__).resolve().parents[1] / "frontend" / "openapi" / "frontend.openapi.json"

    @property
    def browser_runtime_mode(self) -> str:
        return "private-postgresql" if self.settings.postgresql_reads_enabled else "private-disabled"

    @staticmethod
    def _board_config(settings: FrontendRuntimeSettings) -> FrontendBoardConfig | None:
        if not settings.postgresql_reads_enabled:
            return None
        if settings.projection_delay_seconds is None:
            raise FrontendSecurityConfigurationError(
                "DISH_FRONTEND_PROJECTION_DELAY_SECONDS is required when PostgreSQL reads are enabled"
            )
        return FrontendBoardConfig(
            first_page_size=50,
            continuation_page_size=50,
            max_sections=100,
            projection_delay=timedelta(seconds=settings.projection_delay_seconds),
        )

    def startup_check(self) -> None:
        self.auth.startup_check()
        auth_identity = self._validate_database_connection(
            self.auth_factory,
            unavailable_message="private frontend authentication PostgreSQL is unavailable",
        )
        if self.settings.postgresql_reads_enabled:
            observation_identity = self._validate_database_connection(
                self._required_observation_factory(),
                read_only=True,
                unavailable_message="private frontend observation PostgreSQL is unavailable",
            )
            if auth_identity.is_same_physical_database(observation_identity):
                raise FrontendSecurityConfigurationError(
                    "frontend authentication and observation data must use different "
                    "physical PostgreSQL databases"
                )

    def board(self) -> dict[str, Any]:
        return self._board_read(lambda service: service.bootstrap())

    def search(self, query: str) -> dict[str, Any]:
        return self._board_read(lambda service: service.search(query))

    def admin(self) -> dict[str, Any]:
        config = self._required_board_config()
        session = self._required_observation_factory()()
        try:
            with session.begin():
                session.execute(text("SET TRANSACTION READ ONLY"))
                service = FrontendAdminService(
                    FrontendAdminQuery(session),
                    environment=_PRIVATE_ENVIRONMENT,
                    config=FrontendAdminConfig(projection_delay=config.projection_delay),
                )
                return service.read()
        except SQLAlchemyError as exc:
            raise BoardReadUnavailable("private PostgreSQL admin read is unavailable") from exc
        finally:
            session.close()

    def continuation(self, *, section_route_id: str, cursor: str) -> dict[str, Any]:
        return self._board_read(lambda service: service.continuation(section_route_id=section_route_id, cursor=cursor))

    def detail(self, *, task_route_id: str) -> dict[str, Any]:
        config = self._required_board_config()
        session = self._required_observation_factory()()
        try:
            with session.begin():
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                service = FrontendDetailService(
                    FrontendDetailQuery(session),
                    environment=_PRIVATE_ENVIRONMENT,
                    token_secret=self.settings.token_secret,  # type: ignore[arg-type]
                    config=FrontendDetailConfig(
                        projection_delay=config.projection_delay,
                        max_route_candidates=5000,
                    ),
                )
                facts = service.capture(task_route_id)
            return service.present(facts)
        except SQLAlchemyError as exc:
            raise BoardReadUnavailable("private PostgreSQL read is unavailable") from exc
        finally:
            session.close()

    def openapi_document(self) -> dict[str, Any]:
        return json.loads(self.frontend_openapi.read_text(encoding="utf-8"))

    def close(self) -> None:
        if self.observation_engine is not None:
            self.observation_engine.dispose()
        self.auth_engine.dispose()

    def _required_board_config(self) -> FrontendBoardConfig:
        if self.board_config is None:
            raise FrontendDataReadsDisabled("private PostgreSQL observation reads are not activated")
        return self.board_config

    def _required_observation_factory(self) -> sessionmaker[Session]:
        if self.observation_factory is None:
            raise FrontendDataReadsDisabled("private PostgreSQL observation reads are not activated")
        return self.observation_factory

    @staticmethod
    def _validate_database_connection(
        factory: sessionmaker[Session],
        *,
        read_only: bool = False,
        unavailable_message: str,
    ) -> _PostgreSQLDatabaseIdentity:
        try:
            with factory.begin() as session:
                if session.get_bind().dialect.name != "postgresql":
                    raise FrontendSecurityConfigurationError("private frontend requires PostgreSQL")
                if read_only:
                    session.execute(text("SET TRANSACTION READ ONLY"))
                row = session.execute(
                    text(
                        "SELECT current_database(), "
                        "(SELECT oid::bigint FROM pg_database WHERE datname = current_database())"
                    )
                ).one()
                database_oid = row[1]
                if database_oid is None:
                    raise FrontendSecurityConfigurationError(
                        "private frontend could not determine PostgreSQL database identity"
                    )
                return _PostgreSQLDatabaseIdentity(
                    database_name=str(row[0]),
                    database_oid=int(database_oid),
                )
        except SQLAlchemyError as exc:
            raise FrontendSecurityConfigurationError(unavailable_message) from exc

    def _board_read(self, operation):
        config = self._required_board_config()
        session = self._required_observation_factory()()
        try:
            with session.begin():
                session.execute(text("SET TRANSACTION READ ONLY"))
                service = FrontendBoardService(
                    FrontendBoardQuery(session),
                    environment=_PRIVATE_ENVIRONMENT,
                    token_secret=self.settings.token_secret,  # type: ignore[arg-type]
                    config=config,
                )
                return operation(service)
        except SQLAlchemyError as exc:
            raise BoardReadUnavailable("private PostgreSQL read is unavailable") from exc
        finally:
            session.close()
