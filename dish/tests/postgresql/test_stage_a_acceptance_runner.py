from __future__ import annotations

import runpy

import pytest
from pathlib import Path


pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]


def test_stage_a_acceptance_selection_pins_all_release_safety_owners() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-acceptance"))
    selectors = tuple(namespace["FOCUSED_TEST_SELECTORS"])
    assert selectors == (
        "stage1",
        "stage2",
        "stage3",
        "stage4",
        "stage5",
        "stage6",
        "stage7",
        "stage8",
        "fail_closed_admission_outbox",
        "frozen_migration_history",
        "release_evidence_contracts",
        "command_effect_authority",
        "postgresql_action_openapi_oracle",
        "stage_a_release_decomposition",
        "stage_a_acceptance_runner",
    )
    assert namespace["FOCUSED_TEST_EXPRESSION"] == " or ".join(selectors)


def test_stage_a_acceptance_report_names_selection_metadata(monkeypatch, tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-acceptance"))
    captured: dict[str, object] = {}

    def fake_run(command: list[str]) -> dict[str, object]:
        return {
            "command": command,
            "exit_code": 0,
            "duration_seconds": 0.0,
            "output_sha256": "0" * 64,
            "output": "",
        }

    def fake_write(path: Path, value: object) -> None:
        captured["path"] = path
        captured["report"] = value

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_run", fake_run)
    monkeypatch.setitem(main.__globals__, "_source_manifest", lambda: ([], "a" * 64))
    monkeypatch.setitem(main.__globals__, "_write_atomic", fake_write)
    result = main(["--output", str(tmp_path / "report.json"), "--skip-full"])
    assert result == 0
    report = captured["report"]
    assert isinstance(report, dict)
    assert report["format"] == "dish-stage-a-acceptance-report-v2"
    assert report["focused_test_selectors"] == list(namespace["FOCUSED_TEST_SELECTORS"])
    assert report["gates"][0]["command"][-1] == namespace["FOCUSED_TEST_EXPRESSION"]


def test_stage_a_source_manifest_excludes_local_generated_state(monkeypatch, tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-acceptance"))
    (tmp_path / "source.py").write_text("source\n", encoding="utf-8")
    for directory in (".venv-current", ".test-artifacts", ".pytest_cache"):
        path = tmp_path / directory
        path.mkdir()
        (path / "generated.py").write_text("generated\n", encoding="utf-8")
    (tmp_path / ".coverage").write_text("coverage\n", encoding="utf-8")
    source_manifest = namespace["_source_manifest"]
    monkeypatch.setitem(source_manifest.__globals__, "ROOT", tmp_path)
    rows, _digest = source_manifest()
    assert [row["path"] for row in rows] == ["source.py"]
