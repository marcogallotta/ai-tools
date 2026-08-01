"""Typed state bundles replace implicit long-parameter and dictionary contracts."""

from __future__ import annotations

import inspect

import pytest

from dish_service.restore_plan import RestorePlan
from dish_tool.abandonment_succession import AbandonmentSuccessionSpec
from dish_tool.database import apply_operation_abandonment_succession_in_transaction
from dish_tool.errors import DishRuleError


def _succession_spec() -> AbandonmentSuccessionSpec:
    return AbandonmentSuccessionSpec(
        abandonment_id="a",
        succession_id="s",
        successor_operation_id="o",
        source_content_version_id="source",
        successor_content_version_id="successor",
        successor_operation_kind="initial",
        successor_phase="prepare_required",
        successor_expected_section_gid="research",
        successor_schema_version="2",
        successor_claim_mode="stage_actor",
        transition_reason="test",
        candidate_transfer_kind="restored_stage_baseline",
        successor_actor_facts=[{"role": "researcher", "agent": "gpt"}],
        successor_completed_steps={"intent": {"outcome": "completed"}},
    )


def test_abandonment_succession_accepts_one_typed_specification():
    parameters = list(
        inspect.signature(
            apply_operation_abandonment_succession_in_transaction
        ).parameters
    )
    assert parameters == ["conn", "spec"]

    spec = _succession_spec()
    normalized = spec.normalized()
    assert normalized.created_at
    assert isinstance(normalized.successor_actor_facts, tuple)
    assert normalized.successor_completed_steps == {
        "intent": {"outcome": "completed"}
    }


def test_restore_plan_round_trips_only_declared_checkpoint_fields():
    plan = RestorePlan(
        backup_id="dish-backup.sqlite3",
        source={"sha256": "abc", "size_bytes": 1},
        candidate={"path": "/tmp/candidate"},
        live_at_start={"main": None},
    )
    restored = RestorePlan.from_mapping(plan.as_dict())
    assert restored.as_dict() == plan.as_dict()

    with pytest.raises(KeyError, match="unknown restore-plan field"):
        plan["accidental_stage_flag"] = True


def test_restore_plan_rejects_unknown_durable_checkpoint_fields():
    with pytest.raises(DishRuleError) as caught:
        RestorePlan.from_mapping(
            {
                "backup_id": "dish-backup.sqlite3",
                "candidate": {"path": "/tmp/candidate"},
                "legacy_guess": True,
            }
        )
    assert caught.value.rule == "backup_restore_recovery_checkpoint_invalid"
