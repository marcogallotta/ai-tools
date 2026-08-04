"""Reusable populated-predecessor migration test infrastructure."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import make_url

from tests.support.postgresql.certification import probe_native_postgresql

ROOT = Path(__file__).resolve().parents[3]
T = TypeVar("T")


@dataclass(frozen=True)
class MigrationDatabase:
    """An isolated Alembic target with explicit evidence classification."""

    sqlalchemy_url: str
    expected_dialect: str
    certification_evidence: bool = False
    lane: str = "compatibility"

    def alembic_config(self) -> Config:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", self.sqlalchemy_url)
        return config

    def create_engine(self) -> Engine:
        options: dict[str, object] = {"future": True}
        if self.lane == "pglite_development":
            # PGlite's TCP shim shares prepared-statement state differently from
            # native PostgreSQL; disable psycopg auto-prepare and pooling so
            # lifecycle failures are not mistaken for migration assertions.
            options["connect_args"] = {"prepare_threshold": None}
            options["poolclass"] = NullPool
        engine = create_engine(self.sqlalchemy_url, **options)
        if self.expected_dialect == "sqlite":
            @event.listens_for(engine, "connect")
            def _sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
                dbapi_connection.execute("PRAGMA foreign_keys = ON")
        actual = engine.dialect.name
        if actual != self.expected_dialect:
            engine.dispose()
            raise AssertionError(
                f"migration lane {self.lane!r} expected dialect {self.expected_dialect!r}, "
                f"got {actual!r}"
            )
        if self.certification_evidence:
            probe_native_postgresql(self.sqlalchemy_url)
        return engine

    def reset(self) -> None:
        """Return the isolated target to a truly empty database/schema."""

        url = make_url(self.sqlalchemy_url)
        if self.expected_dialect == "sqlite":
            database = url.database
            if not database or database == ":memory:":
                raise ValueError("SQLite migration tests require an isolated file database")
            path = Path(database)
            path.unlink(missing_ok=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            return

        engine = self.create_engine()
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
                connection.exec_driver_sql("CREATE SCHEMA public")
        finally:
            engine.dispose()

    def initialize(self, predecessor_revision: str) -> None:
        """Reset and upgrade through the exact predecessor revision."""

        self.reset()
        command.upgrade(self.alembic_config(), predecessor_revision)
        self.assert_revision(predecessor_revision)

    def stamp(self, revision: str) -> None:
        """Stamp an intentionally hand-built predecessor schema."""

        command.stamp(self.alembic_config(), revision)
        self.assert_revision(revision)

    def upgrade(self, target_revision: str = "head") -> None:
        command.upgrade(self.alembic_config(), target_revision)

    def fresh_bootstrap(self, target_revision: str = "head") -> None:
        self.reset()
        self.upgrade(target_revision)

    def assert_revision(self, expected_revision: str) -> None:
        actual = self.current_revision()
        if actual != expected_revision:
            raise AssertionError(
                f"expected Alembic revision {expected_revision!r}, got {actual!r}"
            )

    def current_revision(self) -> str | None:
        engine = self.create_engine()
        try:
            with engine.connect() as connection:
                exists = connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name='alembic_version'"
                    )
                ).scalar_one() if self.expected_dialect == "postgresql" else connection.execute(
                    text(
                        "SELECT count(*) FROM sqlite_master "
                        "WHERE type='table' AND name='alembic_version'"
                    )
                ).scalar_one()
                if not exists:
                    return None
                return connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        finally:
            engine.dispose()

    def seed(self, seeder: Callable[[Connection], T]) -> T:
        engine = self.create_engine()
        try:
            with engine.begin() as connection:
                return seeder(connection)
        finally:
            engine.dispose()

    def read(self, reader: Callable[[Connection], T]) -> T:
        engine = self.create_engine()
        try:
            with engine.connect() as connection:
                return reader(connection)
        finally:
            engine.dispose()

    def expect_upgrade_failure(
        self,
        target_revision: str,
        *,
        expected_exception: type[BaseException] = Exception,
        message_fragment: str | None = None,
    ) -> BaseException:
        try:
            self.upgrade(target_revision)
        except expected_exception as exc:
            if message_fragment is not None and message_fragment not in str(exc):
                raise AssertionError(
                    f"upgrade failed with unexpected message: {exc}"
                ) from exc
            return exc
        raise AssertionError(
            f"upgrade to {target_revision!r} unexpectedly accepted invalid predecessor data"
        )

    @contextmanager
    def independent_connections(self, count: int = 2) -> Iterator[tuple[Connection, ...]]:
        """Open independent connections for lock/CAS/transaction barrier tests."""

        if count < 2:
            raise ValueError("independent connection tests require at least two connections")
        engine = self.create_engine()
        connections = tuple(engine.connect() for _ in range(count))
        try:
            yield connections
        finally:
            for connection in reversed(connections):
                connection.close()
            engine.dispose()
