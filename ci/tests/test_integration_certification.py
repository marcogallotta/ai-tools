from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "integration_certification.py"


def _module():
    spec = importlib.util.spec_from_file_location("integration_certification", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _target(
    target_id: str, boundary: str, *, requirements: list[str] | None = None,
    command: str | None = None,
) -> dict[str, object]:
    return {
        "id": target_id,
        "execution_boundary": boundary,
        "requirements": requirements or [],
        "commands": [{"name": target_id, "argv": [command or target_id]}],
    }


def _write_spec(tmp_path: Path, targets: list[dict[str, object]]) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({
        "schema": "dish-certification-execution-spec-v2",
        "candidate_sha": "a" * 40,
        "plan_digest": "b" * 64,
        "targets": targets,
    }), encoding="utf-8")
    return path


def test_runtime_requirements_come_from_targets_not_group_labels(tmp_path: Path) -> None:
    module = _module()
    spec = module.load_execution_spec(_write_spec(tmp_path, [
        _target("frontend", "frontend-static", requirements=["node"]),
        _target("native", "native-postgresql", requirements=["python", "postgresql"]),
    ]))
    assert spec.required_groups == ("frontend-static", "native-postgresql")
    assert module.setup_requirements(spec) == {
        "python": True,
        "node": True,
        "postgresql": True,
        "chromium": False,
        "flake": False,
    }


def test_execution_is_target_ordered_and_fail_fast(tmp_path: Path) -> None:
    module = _module()
    spec = module.load_execution_spec(_write_spec(tmp_path, [
        _target("browser-z", "browser-acceptance"),
        _target("python-b", "python-control-plane"),
        _target("python-a", "python-control-plane"),
        _target("native", "native-postgresql"),
    ]))
    called: list[str] = []
    times = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

    def run(command, _root, _log):
        called.append(command.name)
        return 1 if command.name == "python-b" else 0

    evidence = module.execute_certification(
        spec, run_id="42", run_attempt=2, repo_root=ROOT,
        evidence_path=tmp_path / "evidence.json", command_runner=run,
        clock=lambda: next(times),
    )
    assert called == ["python-a", "python-b"]
    assert evidence["execution_order"] == ["python-a", "python-b", "native", "browser-z"]
    assert evidence["target_results"]["python-a"]["result"] == "passed"
    assert evidence["target_results"]["python-b"]["result"] == "failed"
    assert evidence["target_results"]["native"]["result"] == "not_run_due_to_prior_failure"
    assert evidence["outcome"] == "failed"
    assert json.loads((tmp_path / "evidence.json").read_text())["schema"] == "dish-integration-certification-v2"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(schema="old"), "execution spec schema"),
        (lambda value: value["targets"][0].update(execution_boundary="mystery"), "unknown execution boundary"),
        (lambda value: value["targets"][0].update(requirements=["python", "python"]), "requirements must be unique"),
        (lambda value: value["targets"][0]["commands"][0].update(argv="pytest -q"), "argv must be"),
        (lambda value: value["targets"][0]["commands"][0].update(cwd="../escape"), "cwd must be canonical"),
    ],
)
def test_execution_spec_rejects_malformed_target_contract(tmp_path: Path, mutate, message: str) -> None:
    module = _module()
    raw = {
        "schema": "dish-certification-execution-spec-v2",
        "candidate_sha": "a" * 40,
        "plan_digest": "b" * 64,
        "targets": [_target("python", "python-control-plane", requirements=["python"])],
    }
    mutate(raw)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(module.CertificationError, match=message):
        module.load_execution_spec(path)
