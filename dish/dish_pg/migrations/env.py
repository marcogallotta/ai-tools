from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from dish_pg.models import Base
from dish_pg import stage3_models  # noqa: F401 -- register Stage 3 metadata
from dish_pg import stage5_models  # noqa: F401 -- register Stage 5 metadata
from dish_pg import stage6_models  # noqa: F401 -- register Stage 6 metadata
from dish_pg import reservation_models  # noqa: F401 -- register first-request reservation metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=False,
        output_buffer=config.attributes.get("output_buffer"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_on_connection(connection) -> None:
    connection.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS alembic_version "
        "(version_num VARCHAR(128) NOT NULL PRIMARY KEY)"
    )
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        _run_on_connection(supplied_connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    # Own one outer transaction so the explicit version-table bootstrap and
    # every Alembic revision commit together. A plain connection context rolls
    # back its implicit transaction on close under PostgreSQL.
    with connectable.begin() as connection:
        _run_on_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

