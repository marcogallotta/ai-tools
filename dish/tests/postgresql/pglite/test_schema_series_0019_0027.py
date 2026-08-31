"""Combined populated-predecessor coverage for the 0019-0027 schema series."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from tests.support.postgresql.pglite_fixtures import (
    insert_generation,
    insert_request,
    insert_run,
    upgrade_on,
)

pytestmark = pytest.mark.pglite




def test_populated_0018_predecessor_upgrades_linearly_to_0027(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(
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
            insert_generation(raw, generation_id)
            insert_run(
                raw,
                generation_id=generation_id,
                run_id=run_id,
                owner_id="owner-a",
                digest_byte=b"c",
            )
            insert_request(
                raw,
                generation_id=generation_id,
                request_id=request_id,
                run_id=run_id,
                owner_id="owner-a",
            )
            raw.autocommit = False
            # This fixture certifies only the named 0019-0027 historical
            # series; later cutover guards reject its synthetic live rows.
            upgrade_on(connection, pglite.sqlalchemy_url, "0027_server_default_alignment")
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
                        'projection_worker_readiness'
                    )"""
                )
            ).scalar_one()
            assert table_count == 6
    finally:
        engine.dispose()
