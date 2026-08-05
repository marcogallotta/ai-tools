"""Metadata/server-default comparison coverage for durable raw-SQL defaults."""
from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from dish_pg.models import Base
from tests.support.postgresql.core import ROOT

from tests.support.postgresql.pglite_fixtures import upgrade_on

pytestmark = pytest.mark.pglite




def test_0027_server_defaults_match_orm_metadata_with_default_comparison(pglite):
    engine=create_engine(pglite.sqlalchemy_url,future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "0027_server_default_alignment"); connection.commit()
            context=MigrationContext.configure(connection,opts={"compare_server_default":True,"compare_type":True,"target_metadata":Base.metadata})
            diffs=compare_metadata(context,Base.metadata)
            protected={
                ("shadow_envelopes","capture_qualification"),
                ("shadow_envelopes","envelope_schema_version"),
                ("projection_epochs","external_effects_enabled"),
                ("projection_outbox_events","origin"),
            }
            default_diffs=[]
            def walk(value):
                if isinstance(value,list):
                    for item in value: walk(item)
                elif isinstance(value,tuple):
                    if value and value[0]=="modify_default" and (value[2],value[3]) in protected:
                        default_diffs.append(value)
                    for item in value:
                        if isinstance(item,(list,tuple)): walk(item)
            walk(diffs)
            assert default_diffs == []
            db={}
            inspector=inspect(connection)
            for table,column in protected:
                db[(table,column)]=next(c for c in inspector.get_columns(table) if c["name"]==column)["default"]
            assert "legacy" in str(db[("shadow_envelopes","capture_qualification")])
            assert "1" in str(db[("shadow_envelopes","envelope_schema_version")])
            assert "false" in str(db[("projection_epochs","external_effects_enabled")]).lower()
            assert "live" in str(db[("projection_outbox_events","origin")])
    finally:
        engine.dispose()
