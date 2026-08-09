"""PostgreSQL composition for the loopback-only Stage 3 local board."""
from __future__ import annotations

import argparse
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from dish_pg.database import DatabaseSettings, create_database_engine, session_factory
from dish_pg.frontend_board_query import BoardReadUnavailable, FrontendBoardQuery
from dish_pg.frontend_detail_query import FrontendDetailQuery
from dish_service.frontend_board import FrontendBoardConfig, FrontendBoardService
from dish_service.frontend_detail import FrontendDetailConfig, FrontendDetailService
from dish_service.frontend_local_http import (
    FrontendLocalServer,
    LocalBoardBackend,
    is_loopback_host,
)

_LOCAL_ENVIRONMENT = "local-postgresql-observation"
_DISH_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DATABASE_URL = DatabaseSettings().url
_DEFAULT_PROJECTION_DELAY = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class LocalFrontendSettings:
    database_url: str = _DEFAULT_DATABASE_URL
    host: str = "127.0.0.1"
    port: int = 4173
    projection_delay: timedelta = _DEFAULT_PROJECTION_DELAY
    first_page_size: int = 50
    continuation_page_size: int = 50
    max_sections: int = 100
    max_detail_route_candidates: int = 5000

    def __post_init__(self) -> None:
        if not is_loopback_host(self.host):
            raise ValueError("local frontend must bind to a loopback address")
        if not 0 <= self.port <= 65535:
            raise ValueError("local frontend port must be between 0 and 65535")
        # Reuse the Stage 3 service's closed bounds instead of maintaining a
        # second local-only capacity policy.
        if self.max_detail_route_candidates <= 0:
            raise ValueError("max detail route candidates must be positive")
        FrontendBoardConfig(
            first_page_size=self.first_page_size,
            continuation_page_size=self.continuation_page_size,
            max_sections=self.max_sections,
            projection_delay=self.projection_delay,
        )


class PostgresLocalBoardBackend:
    """Own short read-only PostgreSQL transactions for local board requests."""

    def __init__(
        self,
        factory: sessionmaker,
        *,
        token_secret: bytes | None = None,
        config: FrontendBoardConfig,
        max_detail_route_candidates: int = 5000,
    ) -> None:
        self.factory = factory
        self.token_secret = token_secret or secrets.token_bytes(32)
        self.config = config
        self.max_detail_route_candidates = max_detail_route_candidates

    def bootstrap(self) -> dict[str, Any]:
        return self._read(lambda service: service.bootstrap())

    def continuation(self, *, section_route_id: str, cursor: str) -> dict[str, Any]:
        return self._read(
            lambda service: service.continuation(
                section_route_id=section_route_id,
                cursor=cursor,
            )
        )

    def detail(self, *, task_route_id: str) -> dict[str, Any]:
        session = self.factory()
        try:
            with session.begin():
                if session.get_bind().dialect.name != "postgresql":
                    raise BoardReadUnavailable("local frontend requires PostgreSQL")
                # Stage 4 detail captures all factual inputs from one coherent local snapshot.
                # This is local observation wiring, not evidence that the production 3D
                # coherent-read gate has been accepted.
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                service = FrontendDetailService(
                    FrontendDetailQuery(session),
                    environment=_LOCAL_ENVIRONMENT,
                    token_secret=self.token_secret,
                    config=FrontendDetailConfig(
                        projection_delay=self.config.projection_delay,
                        max_route_candidates=self.max_detail_route_candidates,
                    ),
                )
                facts = service.capture(task_route_id)
            return service.present(facts)
        except SQLAlchemyError as exc:
            raise BoardReadUnavailable("local PostgreSQL read is unavailable") from exc
        finally:
            session.close()

    def _read(self, operation):
        session = self.factory()
        try:
            with session.begin():
                if session.get_bind().dialect.name != "postgresql":
                    raise BoardReadUnavailable("local frontend requires PostgreSQL")
                # Defense in depth: this local observation path has no write authority.
                session.execute(text("SET TRANSACTION READ ONLY"))
                service = FrontendBoardService(
                    FrontendBoardQuery(session),
                    environment=_LOCAL_ENVIRONMENT,
                    token_secret=self.token_secret,
                    config=self.config,
                )
                return operation(service)
        except SQLAlchemyError as exc:
            raise BoardReadUnavailable("local PostgreSQL read is unavailable") from exc
        finally:
            session.close()


def build_local_server(
    settings: LocalFrontendSettings,
    *,
    static_root: Path,
) -> tuple[FrontendLocalServer, Any]:
    engine = create_database_engine(DatabaseSettings(url=settings.database_url))
    backend: LocalBoardBackend = PostgresLocalBoardBackend(
        session_factory(engine),
        config=FrontendBoardConfig(
            first_page_size=settings.first_page_size,
            continuation_page_size=settings.continuation_page_size,
            max_sections=settings.max_sections,
            projection_delay=settings.projection_delay,
        ),
        max_detail_route_candidates=settings.max_detail_route_candidates,
    )
    try:
        server = FrontendLocalServer(
            (settings.host, settings.port),
            backend=backend,
            static_root=static_root,
        )
    except Exception:
        engine.dispose()
        raise
    return server, engine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dish-frontend-local",
        description="Serve the Stage 3/4 read-only frontend from non-authoritative local PostgreSQL.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DISH_FRONTEND_LOCAL_DATABASE_URL", DatabaseSettings().url),
        help="local PostgreSQL SQLAlchemy URL (default: repository Compose database)",
    )
    parser.add_argument("--bind", "--host", dest="host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DISH_FRONTEND_LOCAL_PORT", "4173")),
    )
    parser.add_argument(
        "--static-root",
        type=Path,
        default=_DISH_ROOT / "frontend" / "dist",
        help="built frontend directory",
    )
    parser.add_argument("--first-page-size", type=int, default=50)
    parser.add_argument("--continuation-page-size", type=int, default=50)
    parser.add_argument("--max-sections", type=int, default=100)
    parser.add_argument("--max-detail-route-candidates", type=int, default=5000)
    parser.add_argument(
        "--projection-delay-seconds",
        type=int,
        default=int(os.environ.get("DISH_FRONTEND_LOCAL_PROJECTION_DELAY_SECONDS", "900")),
        help=(
            "local observation-only delayed-projection threshold; "
            "not a production contract decision"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = LocalFrontendSettings(
        database_url=args.database_url,
        host=args.host,
        port=args.port,
        projection_delay=timedelta(seconds=args.projection_delay_seconds),
        first_page_size=args.first_page_size,
        continuation_page_size=args.continuation_page_size,
        max_sections=args.max_sections,
        max_detail_route_candidates=args.max_detail_route_candidates,
    )
    server, engine = build_local_server(settings, static_root=args.static_root)
    host, port = server.server_address[:2]
    print(f"Dish local PostgreSQL frontend: http://{host}:{port}/?source=postgresql")
    print(
        "PostgreSQL is a non-authoritative local observation source; "
        "this server has no mutation routes."
    )
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.dispose()
    return 0
