from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "integration_certification.py"
ACTION = ROOT / ".github" / "actions" / "run-certification" / "action.yml"
SCHEMA = ROOT / "ci" / "integration-certification-evidence.schema.json"


def _module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("integration_certification", MODULE_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def _write_plan(tmp_path: Path, selected_groups: list[str]) -> Path:
    plan = {
        "format": "repository-certification-plan-v1",
        "identity": {
            "candidate_sha": "a" * 40,
            "base_sha": "b" * 40,
            "merge_base_sha": "c" * 40,
        },
        "changed_paths": ["example.py"],
        "classifications": [],
        "dish_selector": {},
        "semantic_additions": {"review_complete": True, "lanes": []},
        "selected_lanes": [],
        "selected_groups": selected_groups,
        "force_full": False,
        "force_full_reasons": [],
        "policy_identity": {"format": "repository-certification-policy-identity-v1"},
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def _write_commands(tmp_path: Path, groups: dict[str, list[dict[str, object]]]) -> Path:
    path = tmp_path / "commands.json"
    path.write_text(
        json.dumps({"format": "dish-certification-command-map-v1", "commands": groups}),
        encoding="utf-8",
    )
    return path


def _command(name: str) -> list[dict[str, object]]:
    return [{"name": name, "argv": ["true"]}]


def test_runtime_requirements_come_only_from_landed_planner_selected_groups(tmp_path: Path) -> None:
    module = _module()

    frontend = module.load_plan(_write_plan(tmp_path, ["frontend-static"]))
    assert module.setup_requirements(frontend) == {
        "python": False,
        "node": True,
        "postgresql": False,
        "chromium": False,
    }

    browser = module.load_plan(_write_plan(tmp_path, ["browser-acceptance"]))
    assert module.setup_requirements(browser) == {
        "python": True,
        "node": True,
        "postgresql": False,
        "chromium": True,
    }

    postgres = module.load_plan(_write_plan(tmp_path, ["native-postgresql"]))
    assert module.setup_requirements(postgres) == {
        "python": True,
        "node": False,
        "postgresql": True,
        "chromium": False,
    }


def test_execution_is_deterministic_fail_fast_and_emits_complete_evidence(tmp_path: Path) -> None:
    module = _module()
    plan = module.load_plan(
        _write_plan(
            tmp_path,
            ["python-control-plane", "native-postgresql", "browser-acceptance"],
        )
    )
    commands = module.load_commands(
        _write_commands(
            tmp_path,
            {
                "python-control-plane": _command("python"),
                "native-postgresql": _command("postgres"),
                "browser-acceptance": _command("browser"),
            },
        ),
        selected_groups=plan["selected_groups"],
    )
    calls: list[str] = []

    def runner(command, _root, _log):
        calls.append(command.name)
        return 7 if command.name == "postgres" else 0

    ticks = iter([0.0, 1.0, 3.5, 4.0, 9.0, 9.0])
    evidence_path = tmp_path / "evidence.json"
    payload = module.execute_certification(
        plan,
        commands,
        run_id="12345",
        run_attempt=2,
        repo_root=tmp_path,
        evidence_path=evidence_path,
        command_runner=runner,
        clock=lambda: next(ticks),
    )

    assert calls == ["python", "postgres"]
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
    assert payload["run_id"] == "12345"
    assert payload["run_attempt"] == 2
    assert payload["elapsed_seconds"] == 9.0
    assert payload["outcome"] == "failed"
    assert len(payload["plan_digest"]) == 64
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == payload


def test_runner_rejects_policy_duplication_inputs_and_shell_strings(tmp_path: Path) -> None:
    module = _module()

    with pytest.raises(module.CertificationError, match="planner execution-group order"):
        module.load_plan(_write_plan(tmp_path, ["browser-acceptance", "python-control-plane"]))

    plan = module.load_plan(_write_plan(tmp_path, ["python-control-plane"]))
    commands = _write_commands(
        tmp_path,
        {
            "python-control-plane": [{"name": "bad", "argv": "pytest -q"}],
        },
    )
    with pytest.raises(module.CertificationError, match="argv must be"):
        module.load_commands(commands, selected_groups=plan["selected_groups"])

    commands = _write_commands(
        tmp_path,
        {
            "python-control-plane": _command("python"),
            "browser-acceptance": _command("browser"),
        },
    )
    with pytest.raises(module.CertificationError, match="unselected groups"):
        module.load_commands(commands, selected_groups=plan["selected_groups"])


def test_composite_action_keeps_every_heavy_setup_conditional_and_uses_plan_contract() -> None:
    action = ACTION.read_text(encoding="utf-8")
    assert '--plan "${{ inputs.plan }}"' in action
    assert '--commands "${{ inputs.commands }}"' in action
    assert "uses: actions/setup-python@v6" in action
    assert "uses: actions/setup-node@v6" in action
    assert "uses: ./.github/actions/setup-python-bundle" in action
    assert "postgres:17.10" in action
    assert "playwright install --with-deps chromium" in action
    assert action.count("if: steps.runtime.outputs.python == 'true'") >= 3
    assert action.count("if: steps.runtime.outputs.node == 'true'") == 1
    assert action.count("if: steps.runtime.outputs.postgresql == 'true'") == 1
    assert action.count("if: steps.runtime.outputs.chromium == 'true'") == 1
    assert "runs-on:" not in action
    assert "jobs:" not in action


def test_evidence_schema_is_parseable_and_names_all_terminal_group_states() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["properties"]["format"]["const"] == "dish-integration-certification-v1"
    states = schema["$defs"]["groupResult"]["properties"]["result"]["enum"]
    assert states == [
        "passed",
        "failed",
        "not_selected",
        "not_run_due_to_prior_failure",
    ]
