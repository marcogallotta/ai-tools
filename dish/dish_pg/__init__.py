"""Stage A PostgreSQL authority implementation.

The package remains isolated from the live SQLite/Asana authority until an
explicit authority activation and Stage 6 cutover authorization are completed.
"""

from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from . import stage3_models as stage3_models  # register Stage 3 metadata
from . import stage5_models as stage5_models  # register Stage 5 metadata
from . import stage6_models as stage6_models  # register Stage 6 metadata
from . import reservation_models as reservation_models  # register exact first-request authority
from .services import CoreAuthorityService, ImportedTaskResult, ImportedTaskSpec
from .command_port import CommandCall, CommandResult, PostgresCommandPort
from .read_model import PostgresReadModel
from .transition import ProjectionService, ShadowService, SourceImportService
from .release import CandidateEvaluation, ReleaseCandidateService
from .workflow import (
    ExecutionSpec,
    RequestAdmission,
    RequestSpec,
    StoredOutcome,
    WorkflowAuthorityService,
)

__all__ = [
    "CommandCall",
    "CommandResult",
    "CoreAuthorityService",
    "DatabaseSettings",
    "ExecutionSpec",
    "ImportedTaskResult",
    "ImportedTaskSpec",
    "RequestAdmission",
    "RequestSpec",
    "CandidateEvaluation",
    "ReleaseCandidateService",
    "StoredOutcome",
    "PostgresCommandPort",
    "PostgresReadModel",
    "ProjectionService",
    "ShadowService",
    "SourceImportService",
    "WorkflowAuthorityService",
    "create_database_engine",
    "session_factory",
    "session_scope",
    "stage3_models",
    "stage5_models",
    "stage6_models",
    "reservation_models",
]
