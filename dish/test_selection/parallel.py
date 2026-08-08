"""Experimental pytest-xdist candidate inventory and command rendering."""
from __future__ import annotations

import shlex
from collections.abc import Iterable

from .model import PolicyError

# Static-review candidate inventory only. This is not worker-safety certification: the lane stays
# experimental until repeated xdist evidence exists for the exact selection/environment.
EXPERIMENTAL_PARALLEL_TEST_FILES = (
    "tests/test_pagination.py",
    "tests/test_architecture_knowledge_base.py",
    "tests/test_dish_tool_critical_input_safety.py",
    "tests/test_materiality_persistence_and_audit_contracts.py",
    "tests/test_material_change_authority_matrix.py",
    "tests/test_dish_tool_step2_canonical.py",
    "tests/test_request_identity.py",
    "tests/test_dish_tool_step5_commands.py",
    "tests/test_admin_argument_validation.py",
    "tests/test_task_store_and_backend_negative_contracts.py",
    "tests/test_workflow_policy_fail_closed.py",
    "tests/test_dish_tool_step6_prepare.py",
    "tests/test_dish_tool_step7_verification.py",
    "tests/test_commands.py",
    "tests/test_dish_planning_authority_labels.py",
    "tests/test_planning_intent_confirmation.py",
    "tests/test_verifier_attestation_safety.py",
    "tests/test_action_replay_contract.py",
    "tests/test_dish_tool_step8_routes.py",
    "tests/test_core_models_and_dispatch.py",
    "tests/test_verification_arguments_and_hold_contracts.py",
    "tests/test_prepare_operation_boundaries.py",
    "tests/test_batch_apply.py",
    "tests/test_material_change_grammar.py",
    "tests/test_mutation_tooling.py",
    "tests/test_unicode_planning_labels.py",
)

_EXPERIMENTAL_PARALLEL_SET = frozenset(EXPERIMENTAL_PARALLEL_TEST_FILES)


def experimental_parallel_eligible(test_files: Iterable[str]) -> bool:
    """Return whether a non-empty focused file set is entirely in the candidate inventory."""
    selected = tuple(dict.fromkeys(test_files))
    return bool(selected) and all(path in _EXPERIMENTAL_PARALLEL_SET for path in selected)


def validate_experimental_parallel_files(test_files: Iterable[str]) -> tuple[str, ...]:
    """Validate and normalize an explicit candidate selection, failing closed on unknown files."""
    selected = tuple(dict.fromkeys(path.strip() for path in test_files if path.strip()))
    if not selected:
        return EXPERIMENTAL_PARALLEL_TEST_FILES
    unsupported = sorted(set(selected) - _EXPERIMENTAL_PARALLEL_SET)
    if unsupported:
        raise PolicyError(
            "experimental parallel execution is not reviewed for: " + ", ".join(unsupported)
        )
    return selected


def experimental_parallel_command(test_files: Iterable[str], *, workers: int) -> str:
    """Render the governed optional command for one reviewed focused selection."""
    if workers < 1:
        raise PolicyError("experimental worker count must be at least 1")
    selected = validate_experimental_parallel_files(test_files)
    parts = [
        ".venv/bin/python",
        "scripts/dish-test-lane",
        "experimental-parallel",
        "--workers",
        str(workers),
    ]
    for path in selected:
        parts.extend(("--test-file", path))
    return " ".join(shlex.quote(part) for part in parts)
