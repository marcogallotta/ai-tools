"""SQLAlchemy engine and explicit application transaction ownership."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


class DatabaseSettings(BaseModel):
    """Validated target-database settings.

    The default URL names the isolated Compose test database. Production code
    must supply its own URL and must not reuse the current SQLite path.
    """

    model_config = ConfigDict(frozen=True)

    url: str = Field(
        default="postgresql+psycopg://dish:dish@127.0.0.1:55432/dish_stage_a_test",
        min_length=1,
    )
    echo: bool = False
    pool_pre_ping: bool = True


def create_database_engine(
    settings: DatabaseSettings,
    **overrides: Any,
) -> Engine:
    """Create an engine without opening a connection.

    Callers own engine disposal. Repository methods never commit; application
    services own the session transaction through :func:`session_scope`.
    """

    options: dict[str, Any] = {
        "echo": settings.echo,
        "pool_pre_ping": settings.pool_pre_ping,
        "future": True,
    }
    options.update(overrides)
    return create_engine(settings.url, **options)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Own one explicit authoritative transaction.

    A successful body commits exactly once. Any exception rolls back the whole
    unit. Repositories receive the yielded session and must not commit it.
    """

    session = factory()
    try:
        with session.begin():
            yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
