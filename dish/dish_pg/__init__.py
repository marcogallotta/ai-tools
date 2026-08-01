"""Stage A PostgreSQL authority implementation.

This package is intentionally isolated from the live SQLite/Asana authority until
an explicit authority activation is completed.
"""

from .database import DatabaseSettings, create_database_engine, session_factory, session_scope

__all__ = [
    "DatabaseSettings",
    "create_database_engine",
    "session_factory",
    "session_scope",
]
