"""Reviewed pytest-xdist-safe inventory, qualification, and command rendering."""
from __future__ import annotations

import hashlib
import shlex
from collections.abc import Iterable
from pathlib import Path

from .model import PolicyError

ROOT = Path(__file__).resolve().parents[1]

# This exact inventory completed three clean runs each at -n 2, -n 4, and -n 8 on 2026-08-08
# (565/565 tests each run) after static isolation review. Qualification applies only to this
# allowlist and does not extend to native PostgreSQL, process-boundary, concurrency/recovery,
# migration/backup/restore, shared-service, or production-shaped evidence.
PARALLEL_SAFE_TEST_FILES = (
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

# Qualification is deliberately content-bound. Do not regenerate these hashes automatically.
# After a listed file changes, keep it serial until static isolation review plus repeated xdist
# evidence has been rerun, then explicitly update only that file's reviewed identity here.
PARALLEL_SAFE_FILE_SHA256 = {
    "tests/test_pagination.py": "22f64d30651261d3fae44accc024d0c987d2f966b14a972bafbd8a8c1c8bfb5e",
    "tests/test_architecture_knowledge_base.py": "fb4d805d12f5e66db0ef88f1e703e6064ed4491689c121c2c5dc515c8547db31",
    "tests/test_dish_tool_critical_input_safety.py": "2f7f1098ec77fca6fbe9ab2f124159a94a0b88d5683c7cbc31a88d1a4c48e6ab",
    "tests/test_materiality_persistence_and_audit_contracts.py": "02fa248bf0dd03e925e853c5a7856b9f1ca151c241622896475ab4be54ae2489",
    "tests/test_material_change_authority_matrix.py": "5fa2a1be36c1cf2638b10977eb5c49d2565cb866950e12c0e5a5116ba0f5f074",
    "tests/test_dish_tool_step2_canonical.py": "9eb3e6cf675e87d5563333260a7c949181255ec705da4dd6b9bb8ba1c383305d",
    "tests/test_request_identity.py": "c4bcff6b6974183ded772f68cf10d26d280047ab492fee661191313794e0913e",
    "tests/test_dish_tool_step5_commands.py": "8590610d25bd952d1d12fb01bebefcf0fb095ba7531653a5701188c5b8b0c30b",
    "tests/test_admin_argument_validation.py": "4b2101df8a47e2c560a177028b24badff1a49c0433b95d4f575f7a3f01f15558",
    "tests/test_task_store_and_backend_negative_contracts.py": "15e03f9aec0fadcf24bf03d1c36875ccd85585a9239f2627847a5c7febd0067a",
    "tests/test_workflow_policy_fail_closed.py": "425c8b1bc0c64f2e1c49ca0640841308b01103566faec9d2baf1edfc74b1e908",
    "tests/test_dish_tool_step6_prepare.py": "2117ff4e1d69f4c0123251de3dd6e93f516e0779f85a339b413bbd5c9338324e",
    "tests/test_dish_tool_step7_verification.py": "91fbbf9d3673040ab52e5d41cbfc3abdbd4f38abd5c763a113167063649fa6e8",
    "tests/test_commands.py": "7b54dc519fcdf6a2aeeee0e3809cf281f6a2bb8e6893618b15fd1c99ddcec6ac",
    "tests/test_dish_planning_authority_labels.py": "4a51d825dc34bce96bccd4268b66cd84d451502c5331ad83b4f0fe2228232340",
    "tests/test_planning_intent_confirmation.py": "786e90a6b4b166d9ca77f983697d51b96f9e1c761d564f389396907d74dbe842",
    "tests/test_verifier_attestation_safety.py": "dffa70aab48cdaa18e35d3900a36f3a3a2ee8d8ae1fec5a07deddbc4acb2c22c",
    "tests/test_action_replay_contract.py": "0618acfad49eb1a0f49c57a506feb03dd432a8c69d9ba32dce495682691c07e2",
    "tests/test_dish_tool_step8_routes.py": "72192dac52912f88bf08ba56adf92398c10d8d993f686c9b0cd3126a681ce99d",
    "tests/test_core_models_and_dispatch.py": "ceb41da4c3150c0beaa7630d45b9e4da79abb53b15b0b8b2ac05c74e164d3aa7",
    "tests/test_verification_arguments_and_hold_contracts.py": "de2c0e9301eecaee473b17d7df42ab2d81be7f39d868266d4f83c863f4834aa1",
    "tests/test_prepare_operation_boundaries.py": "62dedb6ab5365bb7562b9ff09f8c3d15384118aae2dc0d937562e595bff2c883",
    "tests/test_batch_apply.py": "0e37cfd7c8f93bb212c294f5f913fc481135487e59749fccb09b9dde4c265110",
    "tests/test_material_change_grammar.py": "47bb20c5ee262a58e7c019e41fa9516c7e01fb7298b0414b8a52f74b1be9ce7d",
    "tests/test_mutation_tooling.py": "3d169601de8cb93925560770834ddf750e43e170ff86d8e91fcdfc81d6fc843c",
    "tests/test_unicode_planning_labels.py": "fa77f9097d33b0744daf766c4ca7562daad82a22f204e2e947b9c177837f3a1e",
}

_PARALLEL_SAFE_SET = frozenset(PARALLEL_SAFE_TEST_FILES)


def _normalize(test_files: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(path.strip() for path in test_files if path.strip()))


def validate_parallel_safe_files(test_files: Iterable[str]) -> tuple[str, ...]:
    """Validate membership only; serial diagnosis remains usable after qualification drift."""
    selected = _normalize(test_files)
    if not selected:
        return PARALLEL_SAFE_TEST_FILES
    unsupported = sorted(set(selected) - _PARALLEL_SAFE_SET)
    if unsupported:
        raise PolicyError(
            "parallel-safe execution is not reviewed for: " + ", ".join(unsupported)
        )
    return selected


def parallel_safe_qualification_drift(test_files: Iterable[str]) -> tuple[str, ...]:
    """Return reviewed files whose current bytes no longer match the qualified identity."""
    selected = _normalize(test_files)
    drifted: list[str] = []
    for path in selected:
        expected = PARALLEL_SAFE_FILE_SHA256.get(path)
        candidate = ROOT / path
        if expected is None or not candidate.is_file():
            drifted.append(path)
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            drifted.append(path)
    return tuple(drifted)


def parallel_safe_blockers(test_files: Iterable[str]) -> tuple[str, ...]:
    """Explain why a focused selection cannot use supported parallel execution."""
    selected = _normalize(test_files)
    if not selected:
        return ("no reviewed focused tests",)
    unsupported = tuple(sorted(set(selected) - _PARALLEL_SAFE_SET))
    reviewed = tuple(path for path in selected if path in _PARALLEL_SAFE_SET)
    drifted = parallel_safe_qualification_drift(reviewed)
    return unsupported + tuple(
        f"{path} (changed since parallel review; requires explicit requalification)"
        for path in drifted
    )


def parallel_safe_eligible(test_files: Iterable[str]) -> bool:
    """Return whether a non-empty focused set is both reviewed and content-qualified."""
    selected = _normalize(test_files)
    return bool(selected) and not parallel_safe_blockers(selected)


def require_parallel_safe_qualification(test_files: Iterable[str]) -> tuple[str, ...]:
    """Fail closed if reviewed file content changed since parallel qualification."""
    selected = validate_parallel_safe_files(test_files)
    drifted = parallel_safe_qualification_drift(selected)
    if drifted:
        raise PolicyError(
            "parallel-safe qualification drift: "
            + ", ".join(drifted)
            + "; file content changed since parallel review. Run the serial selection, repeat "
            "static isolation review and parallel evidence, then explicitly update "
            "PARALLEL_SAFE_FILE_SHA256 to requalify."
        )
    return selected


def parallel_safe_command(test_files: Iterable[str], *, workers: int) -> str:
    """Render the governed opt-in command for one qualified focused selection."""
    if workers < 1:
        raise PolicyError("parallel worker count must be at least 1")
    selected = require_parallel_safe_qualification(test_files)
    parts = [
        ".venv/bin/python",
        "scripts/dish-test-lane",
        "parallel-safe",
        "--workers",
        str(workers),
    ]
    for path in selected:
        parts.extend(("--test-file", path))
    return " ".join(shlex.quote(part) for part in parts)
