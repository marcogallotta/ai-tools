"""PostgreSQL parity for the legacy Small-correction scope (step8.approve_small).

A verifier may sign its own correction only when it is Small. Governed planning
fields, Decisions, quantities, identity, and tool-owned provenance are Large and
must leave the candidate pending a fresh independent Verification run.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from tests.support.postgresql.command import _call, _verification_ready
from tests.support.postgresql.workflow import _next, workflow_db  # noqa: F401


def _approve_small(port, ids, task_id, started, reviewed, file_text):
    return port.execute(
        _call(
            "approve",
            run_id=started["verifier_run"],
            request_id=_next(ids),
            owner="verifier-owner",
            principal="verification",
            arguments={
                "task_id": str(task_id),
                "operation_id": started["operation_id"],
                "agent": "codex",
                "model": "o3",
                "correction": "small",
                "file_text": file_text,
                "reviewed_identity": reviewed.content_identity,
                "semantic_review_complete": True,
                "provenance_complete": True,
            },
        )
    )


def _ready(session, ids, context, task_id):
    port, _author, verifier_run, started, prepared, _inspected = _verification_ready(
        session, ids, context, task_id
    )
    reviewed = session.get(
        models.ContentVersion, uuid.UUID(prepared.data["content_version_id"])
    )
    text = f"{reviewed.title}\n{reviewed.body}"
    return port, reviewed, text, {
        "operation_id": started.data["operation_id"],
        "verifier_run": verifier_run,
    }


@pytest.mark.parametrize(
    "old,new,expected_key,expected",
    [
        ("Locks: Keep crisp", "Locks: Remove crispness constraint", "governed_fields", "Locks"),
        ("Exemptions: None", "Exemptions: [nutrition-kcal] small side", "governed_fields", "Exemptions"),
        ("Priors: None", "Priors: Earlier batch was soggy", "governed_fields", "Priors"),
        ("Purpose: Compare texture", "Purpose: Compare sweetness", "governed_fields", "Purpose"),
        ("100 g test ingredient", "150 g test ingredient", "material_reasons", "quantities"),
        ("Portions: one sitting", "Portions: three sittings", "material_reasons", "quantities"),
        (
            "A compact side dish for testing texture.",
            "A compact corrected side dish for testing texture.",
            "material_reasons",
            "title_or_identity",
        ),
        ("1. Cook it.", "1. Add 20 g salt and cook it.", "material_reasons", "method"),
        ("Crisp and aromatic.", "Soft and mild.", "material_reasons", "success_criteria"),
        (
            "to isolate texture\n",
            "to isolate texture\nHuman — Marco: Authorizes Locks change, 2026-07-26, to relax crispness\n",
            "governed_fields",
            "Decisions",
        ),
    ],
)
def test_small_correction_rejects_large_scope_without_signing(
    workflow_db, old, new, expected_key, expected
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, reviewed, text, started = _ready(session, ids, context, task_id)
        assert old in text
        state_before = session.get(models.DishState, (context["generation_id"], task_id))
        content_before = state_before.current_content_version_id

        result = _approve_small(
            port, ids, task_id, started, reviewed, text.replace(old, new, 1)
        )

        assert not result.ok
        assert result.code == "SMALL_CORRECTION_SCOPE_EXCEEDED"
        assert result.data["rule"] == "large_correction_required"
        assert expected in result.data[expected_key]
        assert "route=large" in result.data["required_action"]
        if expected_key == "governed_fields":
            assert "governed_change_fields" in result.data["required_action"]
            assert "Marco's approval" in result.data["required_action"]
        else:
            assert "governed_change_fields" not in result.data["required_action"]
        assert session.scalar(select(func.count()).select_from(wf.VerificationCorrection)) == 0
        assert session.scalar(select(func.count()).select_from(wf.VerificationSignoff)) == 0
        state_after = session.get(models.DishState, (context["generation_id"], task_id))
        assert state_after.current_content_version_id == content_before


@pytest.mark.parametrize(
    "field,value",
    [
        ("Researched by", "Codex — o3, 2026-07-26"),
        ("Verification protocol release", "deadbeef"),
    ],
)
def test_small_correction_rejects_tool_owned_provenance_change(
    workflow_db, field, value
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, reviewed, text, started = _ready(session, ids, context, task_id)
        line = next(row for row in text.splitlines() if row.startswith(f"{field}: "))
        result = _approve_small(
            port, ids, task_id, started, reviewed, text.replace(line, f"{field}: {value}", 1)
        )
        assert result.code == "SMALL_CORRECTION_SCOPE_EXCEEDED"
        assert field in result.data["fields"]
        assert session.scalar(select(func.count()).select_from(wf.VerificationSignoff)) == 0


def test_small_correction_rejects_material_change_history_rewrite_as_tampering(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, reviewed, text, started = _ready(session, ids, context, task_id)
        old = "tightened hydration — improve crispness — Large"
        assert old in text
        result = _approve_small(
            port,
            ids,
            task_id,
            started,
            reviewed,
            text.replace(old, "tightened hydration — improve crispness — Small", 1),
        )
        assert not result.ok
        assert result.code == "MATERIAL_CHANGE_HISTORY_MODIFIED"
        assert result.data["rule"] == "material_change_history_modified"
        assert "route=large" not in result.data["required_action"]
        assert session.scalar(select(func.count()).select_from(wf.VerificationCorrection)) == 0
        assert session.scalar(select(func.count()).select_from(wf.VerificationSignoff)) == 0


@pytest.mark.parametrize(
    "old,new",
    [
        ("1. Cook it.", "1. Cook it gently."),
        ("1. Cook it.", "1. Cook it.\n2. Keep the crisp component separate until serving."),
        ("1. Cook it.", "1. Cook it.\n2. Finish freshly just before serving."),
        ("A compact side dish", "A compact, side dish"),
    ],
)
def test_small_correction_accepts_handling_only_changes(workflow_db, old, new) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, reviewed, text, started = _ready(session, ids, context, task_id)
        result = _approve_small(
            port, ids, task_id, started, reviewed, text.replace(old, new, 1)
        )
        assert result.ok, result.data
        correction = session.scalar(select(wf.VerificationCorrection))
        assert correction.correction_class == "small"


_WATCH_LINE = "Keep crisp topping separate until serving."


@pytest.mark.parametrize(
    "replacement",
    ["", "Keep crisp topping separate until just before serving."],
    ids=["delete", "rewrite"],
)
def test_small_correction_rejects_watch_out_for_line_removal(
    workflow_db, monkeypatch, replacement
) -> None:
    from tests.support.postgresql import command as command_support

    monkeypatch.setattr(
        command_support,
        "TASK",
        command_support.TASK.replace(
            "## WHAT SUCCESS LOOKS LIKE",
            f"## WATCH OUT FOR\n{_WATCH_LINE}\n## WHAT SUCCESS LOOKS LIKE",
            1,
        ),
    )
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, reviewed, text, started = _ready(session, ids, context, task_id)
        assert _WATCH_LINE in text
        result = _approve_small(
            port, ids, task_id, started, reviewed, text.replace(_WATCH_LINE, replacement, 1)
        )
        assert not result.ok
        assert result.code == "SMALL_CORRECTION_SCOPE_EXCEEDED"
        assert "section:watch_out_for" in result.data["material_reasons"]
        assert "route=large" in result.data["required_action"]
        assert session.scalar(select(func.count()).select_from(wf.VerificationCorrection)) == 0
        assert session.scalar(select(func.count()).select_from(wf.VerificationSignoff)) == 0
