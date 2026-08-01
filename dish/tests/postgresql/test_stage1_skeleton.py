from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from dish_pg.database import DatabaseSettings, create_database_engine, session_factory, session_scope

ROOT = Path(__file__).resolve().parents[2]


def test_stage_a_alembic_lineage_renders_postgresql_sql() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    buffer = io.StringIO()
    config.attributes["output_buffer"] = buffer
    command.upgrade(config, "head", sql=True)
    rendered = buffer.getvalue()
    assert "0001_stage_a_baseline" in rendered
    assert "UPDATE alembic_version" in rendered or "INSERT INTO alembic_version" in rendered


def test_alembic_render_preserves_existing_application_loggers() -> None:
    logger = logging.getLogger("dish.service.application")
    previous_disabled = logger.disabled
    logger.disabled = False
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["output_buffer"] = io.StringIO()
        command.upgrade(config, "head", sql=True)
        assert logger.disabled is False
    finally:
        logger.disabled = previous_disabled


def test_session_scope_owns_commit_and_rollback() -> None:
    engine = create_database_engine(
        DatabaseSettings(url="sqlite+pysqlite:///:memory:", pool_pre_ping=False)
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"))
    factory = session_factory(engine)

    with session_scope(factory) as session:
        session.execute(text("INSERT INTO sample(value) VALUES ('committed')"))

    with pytest.raises(RuntimeError, match="stop"):
        with session_scope(factory) as session:
            session.execute(text("INSERT INTO sample(value) VALUES ('rolled-back')"))
            raise RuntimeError("stop")

    with engine.connect() as connection:
        values = connection.execute(text("SELECT value FROM sample ORDER BY id")).scalars().all()
    assert values == ["committed"]
    engine.dispose()
