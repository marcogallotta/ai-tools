"""Stage A PostgreSQL authority implementation.

This package is intentionally isolated from the live SQLite/Asana authority until
an explicit authority activation is completed.
"""

from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .services import CoreAuthorityService, ImportedTaskResult, ImportedTaskSpec

__all__ = [
    "CoreAuthorityService",
    "DatabaseSettings",
    "ImportedTaskResult",
    "ImportedTaskSpec",
    "create_database_engine",
    "session_factory",
    "session_scope",
]
