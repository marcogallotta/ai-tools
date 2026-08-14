from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "integration_certification.py"
ACTION = ROOT / ".github" / "actions" / "run-certification" / "action.yml"


def _module():
    spec = importlib.util.spec_from_file_location("integration_certification", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_spec(tmp_path: Path, required_groups: dict[str, list[dict[str, object]]]) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(
        json.dumps(
            {
                "schema": "dish-certification-execution-spec-v1",
                "candidate_sha": "a" * 40,
                "plan_digest": "b" * 64,
                "required_groups": required_groups,
            }
        ),
        encoding="utf-8",
    )
    return path


def _command(name: str) -> list[dict[str, object]]:
    return [{"name": name, "argv": ["true"]}]


def test_runtime_requirements_are_derived_only_from_selected_groups(tmp_path: Path) -> None:
    module = _module()

    frontend = module.load_execution_spec(
        _write_spec(tmp_path, {"frontend-static": _command("frontend")})
    )
    assert module.setup_requirements(frontend) == {
        "python": False,
        "node": True,
        "postgresql": False,
        "chromium": False,
        "flake": False,
    }

    browser = module.load_execution_spec(
        _write_spec(tmp_path, {"browser-acceptance": _command("browser")})
    )
    assert module.setup_requirements(browser) == {
        "python": True,
        "node": True,
        "postgresql": False,
        "chromium": True,
        "flake": False,
    }

    postgres = module.load_execution_spec(
        _write_spec(tmp_path, {"native-postgresql": _command("postgres")})
    )
    assert module.setup_requirements(postgres) == {
        "python": True,
        "node": False,
        "postgresql": True,
        "chromium": False,
        "flake": False,
    }


def test_execution_is_deterministic_and_fail_fast_with_complete_group_evidence(tmp_path: Path) -> None:
    module = _module()
    spec = module.load_execution_spec(
        _write_spec(
            tmp_path,
            {
                "browser-acceptance": _command("browser"),
                "native-postgresql": _command("postgres"),
                "python-control-plane": _command("python"),
            },
        )
    )
    calls: list[str] = []

    def runner(command, _root, _log):
        calls.append(command.name)
        return 7 if command.name == "postgres" else 0

    ticks = iter([0.0, 1.0, 3.5, 4.0, 9.0, 9.0])
    evidence_path = tmp_path / "evidence.json"
    payload = module.execute_certification(
        spec,
        run_id="12345",
        run_attempt=2,
        repo_root=tmp_path,
        evidence_path=evidence_path,
        command_runner=runner,
        clock=lambda: next(ticks),
    )

    assert calls == ["python", "postgres"]
    assert payload["execution_order"] == list(module.GROUP_ORDER)
    assert payload["required_groups"] == [
        "python-control-plane",
        "native-postgresql",
        "browser-acceptance",
    ]
    assert payload["group_results"] == {
        "python-control-plane": {"result": "passed", "elapsed_seconds": 2.5},
        "frontend-static": {"result": "not_selected", "elapsed_seconds": 0.0},
        "native-postgresql": {"result": "failed", "elapsed_seconds": 5.0},
        "browser-acceptance": {
            "result": "not_run_due_to_prior_failure",
            "elapsed_seconds": 0.0,
        },
    }
    assert payload["candidate_sha"] == "a" * 40
    assert payload["plan_digest"] == "b" * 64
    assert payload["run_id"] == "12345"
    assert payload["run_attempt"] == 2
    assert payload["elapsed_seconds"] == 9.0
    assert payload["outcome"] == "failed"
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == payload


def test_execution_spec_rejects_shell_strings_and_unknown_groups(tmp_path: Path) -> None:
    module = _module()
    path = _write_spec(
        tmp_path,
        {
            "python-control-plane": [{"name": "bad", "argv": "pytest -q"}],
            "mystery-group": _command("mystery"),
        },
    )
    with pytest.raises(module.CertificationError, match="unknown execution groups"):
        module.load_execution_spec(path)

    path = _write_spec(
        tmp_path,
        {"python-control-plane": [{"name": "bad", "argv": "pytest -q"}]},
    )
    with pytest.raises(module.CertificationError, match="argv must be"):
        module.load_execution_spec(path)


def test_composite_action_keeps_all_heavy_setup_conditional() -> None:
    action = ACTION.read_text(encoding="utf-8")

    assert "uses: actions/setup-python@v6" in action
    assert "uses: actions/setup-node@v6" in action
    assert "uses: ./.github/actions/setup-python-bundle" in action
    assert "postgres:17.10" in action
    assert "playwright install --with-deps chromium" in action

    python_if = "if: steps.runtime.outputs.python == 'true'"
    node_if = "if: steps.runtime.outputs.node == 'true'"
    pg_if = "if: steps.runtime.outputs.postgresql == 'true'"
    chromium_if = "if: steps.runtime.outputs.chromium == 'true'"
    assert action.count(python_if) >= 3
    assert action.count(node_if) == 1
    assert action.count(pg_if) == 1
    assert action.count(chromium_if) == 1
    assert "runs-on:" not in action
    assert "jobs:" not in action
