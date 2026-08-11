from __future__ import annotations

import json
from pathlib import Path

from dish_pg import cutover_activation_rehearsal as rehearsal


def _scenario() -> dict:
    return {
        "scenario": "cutover-activation-checkpoints",
        "evidence": {
            "writer_fence": {
                "stale_process_started_before_engagement": True,
                "stale_process_rejected_after_engagement": True,
            },
            "checkpoint_process_death": [
                {
                    "state": state,
                    "terminated_process_exit_code": -9,
                    "recovery_equal": True,
                    "snapshot": {"state": state},
                }
                for state in rehearsal.REQUIRED_STATES
            ],
        },
    }


def _cases() -> dict[str, dict[str, str]]:
    return {node: {"status": "passed"} for node in rehearsal.TEST_NODES}


def _processes(count: int = 15) -> list[dict[str, object]]:
    return [{"label": f"process-{index}", "final_exit_status": 0} for index in range(count)]


def test_activation_rehearsal_evidence_contract_accepts_exact_checkpoint_sequence() -> None:
    result = rehearsal._validate_evidence(
        cases=_cases(),
        junit_errors=[],
        scenario=_scenario(),
        processes=_processes(),
    )
    assert result == {"ok": True, "errors": [], "process_count": 15}


def test_activation_rehearsal_evidence_contract_rejects_missing_checkpoint() -> None:
    scenario = _scenario()
    scenario["evidence"]["checkpoint_process_death"].pop(3)
    result = rehearsal._validate_evidence(
        cases=_cases(),
        junit_errors=[],
        scenario=scenario,
        processes=_processes(13),
    )
    assert result["ok"] is False
    assert any("checkpoint states" in error for error in result["errors"])
    assert any("process boundaries" in error for error in result["errors"])


def test_activation_rehearsal_blocks_without_native_compose_and_never_substitutes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(rehearsal, "_find_compose_command", lambda **_kwargs: (None, []))
    output = tmp_path / "report.json"
    evidence = tmp_path / "evidence"
    result = rehearsal.main(
        [
            "--output",
            str(output),
            "--evidence-dir",
            str(evidence),
            "--compose-project",
            "dish-stage6-activation-unit",
        ]
    )
    assert result == 3
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert report["ok"] is False
    assert report["native_postgresql_required"] is True
    assert "SQLite/PGlite were not substituted" in report["unavailable_native_evidence"]
    assert report["production_profile_reachable"] is False
    assert report["asana_resources_reachable"] is False
