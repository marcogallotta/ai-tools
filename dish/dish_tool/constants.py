"""Shared constants for the guarded dish workflow."""

import os
from pathlib import Path


COOKING_PROJECT_GID = os.environ.get(
    "DISH_COOKING_PROJECT_GID", "1215089183018968"
)
SOURCING_SECTION_GID = "1215097887456673"
REFERENCE_SECTION_GID = "1215259129474846"
EXCLUDED_SECTION_GIDS = frozenset(
    {SOURCING_SECTION_GID, REFERENCE_SECTION_GID}
)
DEFAULT_DB_PATH = Path("~/.local/state/dish/dish-tool.db").expanduser()
DB_PATH = Path(os.environ.get("DISH_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()

AGENT_FAMILIES = {
    "claude": "claude",
    "gpt": "gpt",
    "codex": "gpt",
}
FAMILIES = frozenset(AGENT_FAMILIES.values())
SUBMISSION_KINDS = frozenset({"planning", "initial", "change"})
CHANGE_LEVELS = frozenset({"small", "large"})
VERIFICATION_ROUTES = frozenset({"small", "large", "evidence", "human-review"})
RESET_CATEGORIES = frozenset({"evidence", "premise", "method", "scope"})
SUBMISSION_STATES = frozenset(
    {
        "drafting",
        "research_handoff",
        "awaiting_verification",
        "awaiting_human",
        "ready",
        "in_flight",
        "written",
        "consumed",
        "discarded",
        "uncertain",
    }
)
TERMINAL_STATES = frozenset({"consumed", "discarded"})
NONTERMINAL_STATES = SUBMISSION_STATES - TERMINAL_STATES

ALLOWED_ACTIONS_BY_STATE = {
    None: [],
    "drafting": ["prepare"],
    "research_handoff": ["prepare"],
    "awaiting_verification": ["approve", "reject"],
    "ready": ["submit"],
    "written": ["submit"],
    "awaiting_human": [],
    "in_flight": [],
    "uncertain": [],
    "consumed": [],
    "discarded": [],
}

EXIT_STATUS_BY_CODE = {
    "OK": 0,
    "INVALID_ARGUMENT": 2,
    "NOT_FOUND": 2,
    "UNMANAGED_TASK": 2,
    "VALIDATION_FAILED": 2,
    "WRONG_STATE": 3,
    "AGENT_MISMATCH": 3,
    "VERIFIER_FAMILY_MISMATCH": 3,
    "CONFLICT": 3,
    "HUMAN_ACTION_REQUIRED": 3,
    "PROTOCOL_INCOMPATIBLE": 3,
    "BACKEND_REJECTED": 4,
    "BACKEND_UNCERTAIN": 5,
    "INTERNAL_ERROR": 1,
}
DEFAULT_RETRYABLE_BY_CODE = {
    "OK": False,
    "INVALID_ARGUMENT": False,
    "NOT_FOUND": False,
    "UNMANAGED_TASK": False,
    "VALIDATION_FAILED": True,
    "WRONG_STATE": False,
    "AGENT_MISMATCH": False,
    "VERIFIER_FAMILY_MISMATCH": False,
    "CONFLICT": False,
    "HUMAN_ACTION_REQUIRED": False,
    "PROTOCOL_INCOMPATIBLE": False,
    "BACKEND_REJECTED": True,
    "BACKEND_UNCERTAIN": False,
    "INTERNAL_ERROR": False,
}

# The SDK is configured with no automatic retries. One request may spend at most
# 10 seconds connecting and 30 seconds waiting for a response. Recovery waits 90
# seconds: 40 seconds maximum request lifetime plus a 30 second safety margin, with
# a further 20 seconds of conservatism for scheduler and clock granularity.
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30
ASANA_REQUEST_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
MAX_REQUEST_LIFETIME_SECONDS = CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS
RECOVERY_SAFETY_MARGIN_SECONDS = 30
RECOVERY_QUARANTINE_SECONDS = 90
if RECOVERY_QUARANTINE_SECONDS <= (
    MAX_REQUEST_LIFETIME_SECONDS + RECOVERY_SAFETY_MARGIN_SECONDS
):
    raise ValueError(
        "RECOVERY_QUARANTINE_SECONDS must exceed max request lifetime plus safety margin"
    )

# Current Honest compatibility contract. These are engine capabilities, not a
# task-pinned protocol release. The exact pair must match Honest/DISH_VERSION.
HONEST_PATH_ENV = "DISH_HONEST_PATH"
DISH_VERSION_FILENAME = "DISH_VERSION"
TASK_SCHEMA_FILENAME = "dish-task-schema.json"
SCHEMA_MIGRATION_DIRECTORY = "dish-schema-migrations"
SUPPORTED_PROTOCOL_VERSION = "1.0.10"
SUPPORTED_TASK_SCHEMA_VERSION = "2"

PROTOCOL_FILENAMES = {
    "planning": "dish-planning-protocol.md",
    "research": "dish-research-protocol.md",
    "verification": "dish-verification-protocol.md",
    "cooking": "dish-cooking-protocol.md",
}

# Retained only as a transitional adapter for the pre-rewrite command lifecycle.
# The authoritative validation configuration is TASK_SCHEMA_FILENAME in Honest.
MANIFEST_FILENAMES = {
    "planning": "planning",
    "complete_task": "complete_task",
}

GOVERNED_PROTOCOL_FILENAMES = tuple(PROTOCOL_FILENAMES.values())
GOVERNED_SCHEMA_FILENAMES = (TASK_SCHEMA_FILENAME,)

# Local SQLite schema version; unrelated to Honest's task SCHEMA_VERSION.
SCHEMA_VERSION = 29
