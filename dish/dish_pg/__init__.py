"""Stage A PostgreSQL authority implementation.

The package remains isolated from the live SQLite/Asana authority until an
explicit authority activation and Stage 6 cutover authorization are completed.
"""

from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from . import stage3_models as stage3_models  # register Stage 3 metadata
from .services import CoreAuthorityService, ImportedTaskResult, ImportedTaskSpec
from .workflow import (
    ExecutionSpec,
    RequestAdmission,
    RequestSpec,
    StoredOutcome,
    WorkflowAuthorityService,
)

__all__ = [
    "CoreAuthorityService",
    "DatabaseSettings",
    "ExecutionSpec",
    "ImportedTaskResult",
    "ImportedTaskSpec",
    "RequestAdmission",
    "RequestSpec",
    "StoredOutcome",
    "WorkflowAuthorityService",
    "create_database_engine",
    "session_factory",
    "session_scope",
    "stage3_models",
]
