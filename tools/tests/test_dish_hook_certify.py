import importlib.machinery
import importlib.util
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    path = ROOT / "tools" / "dish-hook-certify"
    loader = importlib.machinery.SourceFileLoader("dish_hook_certify_test_module", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_rebased_codex_config_targets_exact_candidate_hook_surface(tmp_path):
    module = _load_module()
    output = tmp_path / "hooks.json"
    candidate = Path("/exact/candidate")

    module._rebase_codex_config(output, candidate)

    expected = {
        str(candidate / path)
        for path, item in module.cert.active_hook_surface(ROOT).items()
        if item["boundary"] == "host-adapter" and "codex" in item["hosts"]
    }
    assert set(module._effective_config_command_targets(output)) == expected


def test_candidate_worktree_venv_dependency_fails_before_host_launch(tmp_path, monkeypatch):
    module = _load_module()
    repo = tmp_path / "repo"
    hook = repo / "hooks" / "agent-reground"
    hook.parent.mkdir(parents=True)
    hook.write_text("exec tools/.venv/bin/python script.py\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPO_ROOT", repo)
    requirement = module.cert.HostCertRequirement(("codex",), ("hooks/agent-reground",))

    with pytest.raises(module.HookCertifyError, match="tools/.venv/bin/python"):
        module._preflight_candidate_dependencies(requirement)

    python = repo / "tools" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    module._preflight_candidate_dependencies(requirement)


def test_host_startup_waits_for_output_then_quiet(tmp_path):
    module = _load_module()
    sizes = iter((0, 12, 24, 24, 24, 24))
    current = 0
    drained = 0

    def drain(_seconds):
        nonlocal current, drained
        drained += 1
        current = next(sizes, current)
        time.sleep(0.01)

    module._wait_for_host_startup(
        drain=drain,
        output_size=lambda: current,
        process_alive=lambda: True,
        evidence=tmp_path / "host.log",
        timeout=1.0,
        quiet_seconds=0.02,
    )

    assert drained >= 4


def test_host_startup_fails_if_process_exits_before_output(tmp_path):
    module = _load_module()

    with pytest.raises(module.HookCertifyError, match="exited before interactive startup settled"):
        module._wait_for_host_startup(
            drain=lambda _seconds: None,
            output_size=lambda: 0,
            process_alive=lambda: False,
            evidence=tmp_path / "host.log",
            timeout=1.0,
            quiet_seconds=0.01,
        )
