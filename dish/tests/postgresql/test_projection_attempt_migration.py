from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from dish_pg.release import ALEMBIC_HEAD

pytestmark = pytest.mark.database_boundary
ROOT = Path(__file__).resolve().parents[2]


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    return config


def _projection_attempt_migration_scenario(
    tmp_path: Path,
) -> None:
    path = tmp_path / "projection-attempt-lifecycle.sqlite3"
    config = _config(path)
    command.upgrade(config, "0017_abandonment_terminal_state")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    original_attempt = "1" * 32
    original_event = "2" * 32
    request_hash = "a" * 64
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO projection_attempts (
                        attempt_id,
                        projection_event_id,
                        attempt_number,
                        worker_id,
                        request_identity,
                        intended_external_id,
                        request_payload,
                        request_sha256,
                        state,
                        started_at,
                        terminal_at
                    ) VALUES (
                        :attempt_id,
                        :event_id,
                        1,
                        'legacy-worker',
                        'stable-logical-request',
                        '123456789',
                        '{"notes":"v2"}',
                        :request_hash,
                        'not_applied',
                        '2026-08-03 10:00:00',
                        '2026-08-03 10:00:01'
                    )
                    """
                ),
                {
                    "attempt_id": original_attempt,
                    "event_id": original_event,
                    "request_hash": request_hash,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        with engine.begin() as connection:
            migrated = connection.execute(
                text(
                    """
                    SELECT attempt_kind,
                           predecessor_attempt_id,
                           dispatch_identity,
                           retry_generation,
                           dispatch_claim_token,
                           dispatch_claim_revision,
                           request_identity,
                           state
                      FROM projection_attempts
                     WHERE attempt_id = :attempt_id
                    """
                ),
                {"attempt_id": original_attempt},
            ).mappings().one()
            assert migrated == {
                "attempt_kind": "dispatch",
                "predecessor_attempt_id": None,
                "dispatch_identity": original_attempt + request_hash[:32],
                "retry_generation": 1,
                "dispatch_claim_token": None,
                "dispatch_claim_revision": None,
                "request_identity": "stable-logical-request",
                "state": "not_applied",
            }
            connection.execute(
                text(
                    """
                    INSERT INTO projection_attempts (
                        attempt_id,
                        projection_event_id,
                        attempt_number,
                        attempt_kind,
                        predecessor_attempt_id,
                        worker_id,
                        request_identity,
                        dispatch_identity,
                        retry_generation,
                        dispatch_claim_token,
                        dispatch_claim_revision,
                        intended_external_id,
                        request_payload,
                        request_sha256,
                        state,
                        started_at,
                        terminal_at
                    ) VALUES (
                        :attempt_id,
                        :event_id,
                        1,
                        'dispatch',
                        NULL,
                        'retry-worker',
                        'stable-logical-request',
                        :dispatch_identity,
                        2,
                        :claim_token,
                        7,
                        '123456789',
                        '{"notes":"v2"}',
                        :request_hash,
                        'dispatched',
                        '2026-08-03 10:01:00',
                        NULL
                    )
                    """
                ),
                {
                    "attempt_id": "3" * 32,
                    "event_id": "4" * 32,
                    "dispatch_identity": "b" * 64,
                    "claim_token": "5" * 32,
                    "request_hash": request_hash,
                },
            )
            assert connection.execute(
                text(
                    "SELECT count(*) FROM projection_attempts "
                    "WHERE request_identity = 'stable-logical-request'"
                )
            ).scalar_one() == 2
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ALEMBIC_HEAD
    finally:
        engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade projection retry generations",
    ):
        command.downgrade(config, "0017_abandonment_terminal_state")


def test_projection_attempt_migration_backfills_history_and_allows_versioned_retry(
    tmp_path: Path,
) -> None:
    _projection_attempt_migration_scenario(tmp_path)
