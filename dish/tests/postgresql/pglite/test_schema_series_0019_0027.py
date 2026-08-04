"""Combined populated-predecessor coverage for the 0019-0027 schema series."""
from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from tests.postgresql.pglite.test_0019_request_run_owner_consistency import (
    _insert_generation,
    _insert_request,
    _insert_run,
)
from tests.support.postgresql.core import ROOT

pytestmark = pytest.mark.pglite


def _upgrade_on(connection, url: str, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def test_populated_0018_predecessor_upgrades_linearly_to_0027(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            _upgrade_on(
                connection,
                pglite.sqlalchemy_url,
                "0018_projection_attempt_lifecycle",
            )
            connection.commit()
            raw = connection.connection.driver_connection
            raw.autocommit = True
            generation_id = uuid.uuid4()
            run_id = uuid.uuid4()
            request_id = uuid.uuid4()
            _insert_generation(raw, generation_id)
            _insert_run(
                raw,
                generation_id=generation_id,
                run_id=run_id,
                owner_id="owner-a",
                digest_byte=b"c",
            )
            _insert_request(
                raw,
                generation_id=generation_id,
                request_id=request_id,
                run_id=run_id,
                owner_id="owner-a",
            )
            raw.autocommit = False
            _upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0027_server_default_alignment"
            assert connection.execute(
                text(
                    "SELECT owner_id FROM service_requests "
                    "WHERE request_id=:request_id"
                ),
                {"request_id": request_id},
            ).scalar_one() == "owner-a"
            table_count = connection.execute(
                text(
                    """SELECT count(*) FROM information_schema.tables
                    WHERE table_schema='public' AND table_name IN (
                        'first_request_reservations',
                        'writer_fence_artifact_observations',
                        'release_candidate_manifests',
                        'legacy_request_tombstones',
                        'source_import_native_links',
                        'worker_probe_inventories'
                    )"""
                )
            ).scalar_one()
            assert table_count == 6
    finally:
        engine.dispose()
