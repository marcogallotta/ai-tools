from __future__ import annotations
import hashlib
import uuid
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_port import PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.read_model import PostgresReadModel
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.release import _prepare_candidate
from tests.support.postgresql.abandonment_terminal_migration import build_0016_abandonment_fixture
from tests.support.postgresql.command import (
    _add_verification_queue,
    _call,
    _inspect,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
    _verification_ready,
)



def test_abandonment_terminal_migration_completes_only_durable_published_success(tmp_path) -> None:
    engine, config, durable_id, orphan_id, published_at = build_0016_abandonment_fixture(tmp_path)
    command.upgrade(config, "0017_abandonment_terminal_state")

    with engine.connect() as connection:
        rows = {
            row.abandonment_id: row
            for row in connection.execute(
                text(
                    "SELECT abandonment_id, state, terminal_at FROM abandonment_attempts "
                    "WHERE abandonment_id IN (:durable_id, :orphan_id)"
                ),
                {"durable_id": durable_id.hex, "orphan_id": orphan_id.hex},
            )
        }
        assert rows[durable_id.hex].state == "completed"
        assert rows[durable_id.hex].terminal_at == str(published_at)
        assert rows[orphan_id.hex].state == "published"
        assert rows[orphan_id.hex].terminal_at is None
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0017_abandonment_terminal_state"
