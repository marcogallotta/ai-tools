"""Structural contracts for neutral read-only submission authority."""
from __future__ import annotations

from pathlib import Path

from dish_tool import step9
from dish_tool.submission_authority import (
    approved_signoff,
    latest_destination_failure,
    submission_identity_evidence,
)

ROOT = Path(__file__).resolve().parents[1]


def test_action_snapshot_does_not_depend_on_stage9_execution_module() -> None:
    application_source = (ROOT / "dish_tool" / "application_service.py").read_text(
        encoding="utf-8"
    )
    snapshot_source = (ROOT / "dish_tool" / "workflow_snapshot.py").read_text(
        encoding="utf-8"
    )
    assert "from .workflow_snapshot import build_workflow_snapshot" in application_source
    assert "from .step9 import" not in application_source
    assert "from .submission_authority import submission_authority_facts" in snapshot_source
    assert "from .step9 import" not in snapshot_source


def test_stage9_keeps_compatibility_surface_backed_by_neutral_authority() -> None:
    assert step9.submission_identity_evidence is submission_identity_evidence
    assert step9.latest_destination_failure is latest_destination_failure
    assert step9._authority_approved_signoff is approved_signoff
