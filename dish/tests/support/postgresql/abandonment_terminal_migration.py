"""Real-0016 abandonment migration fixture helpers."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def build_0016_abandonment_fixture(tmp_path: Path):
    database = tmp_path / "abandonment-0016.db"
    url = f"sqlite:///{database}"
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0016_honest_binding_null_identity")
    engine = create_engine(url, future=True)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    durable_id, orphan_id = uuid.uuid4(), uuid.uuid4()
    durable_successor, orphan_successor = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        for abandonment_id, successor_id in ((durable_id, durable_successor), (orphan_id, orphan_successor)):
            values = {name: uuid.uuid4().hex for name in (
                "generation_id", "task_id", "source_operation_id", "source_lease_id",
                "source_run_id", "baseline_content_activation_id", "baseline_placement_event_id",
                "request_id", "command_execution_id",
            )}
            connection.execute(text("""
                INSERT INTO abandonment_attempts (
                    abandonment_id,generation_id,task_id,source_operation_id,source_lease_id,
                    source_actor_attempt_sequence,source_cycle_id,source_owner_id,source_run_id,
                    baseline_content_activation_id,baseline_placement_event_id,reason,state,
                    request_id,command_execution_id,successor_operation_id,created_at,terminal_at
                ) VALUES (
                    :abandonment_id,:generation_id,:task_id,:source_operation_id,:source_lease_id,
                    1,NULL,'owner-1',:source_run_id,:baseline_content_activation_id,
                    :baseline_placement_event_id,'durable fixture','published',:request_id,
                    :command_execution_id,:successor_operation_id,:created_at,NULL
                )
            """), {**values, "abandonment_id": abandonment_id.hex,
                     "successor_operation_id": successor_id.hex, "created_at": now})
            if abandonment_id == durable_id:
                connection.execute(text("""
                    INSERT INTO operation_succession_edges (
                        succession_id,abandonment_id,task_id,source_operation_id,
                        successor_operation_id,claim_mode,prepared_cycle_id,
                        published_by_execution_id,published_at
                    ) VALUES (:succession_id,:abandonment_id,:task_id,:source_operation_id,
                              :successor_operation_id,'operation',NULL,
                              :published_by_execution_id,:published_at)
                """), {
                    "succession_id": uuid.uuid4().hex,
                    "abandonment_id": abandonment_id.hex,
                    "task_id": values["task_id"],
                    "source_operation_id": values["source_operation_id"],
                    "successor_operation_id": successor_id.hex,
                    "published_by_execution_id": values["command_execution_id"],
                    "published_at": now,
                })
    return engine, config, durable_id, orphan_id, now
