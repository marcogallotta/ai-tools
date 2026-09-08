from __future__ import annotations

import json
from pathlib import Path


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "investigation_guard"


def _identity(root: Path, session: str = "session-1", task: str = "1234567890", role: str = "implementation") -> None:
    path = root / "agents" / f"{session}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent_id": session, "owning_task_gid": task, "role": role}) + "\n")


def _policy(root: Path, *, host: str = "claude", task_class: str = "standard", cap: int = 2,
            proof: int = 1, tools: tuple[str, ...] = ("Read", "Grep", "Glob", "Bash", "Edit", "Write"),
            early: bool = False) -> Path:
    path = root / "policy.json"
    value = {
        "schema_version": 1,
        "calibrations": {
            f"{host}:{task_class}": {
                "investigation_cap": cap,
                "proof_tail_cap": proof,
                "qualified_tools": list(tools),
                "early_action_ready": early,
                "trace_digest": "test",
            }
        },
    }
    path.write_text(json.dumps(value) + "\n")
    return path


def _payload(tool: str, **tool_input):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "tool_name": tool,
        "cwd": "/repo",
        "tool_input": tool_input,
    }


def _setup(monkeypatch, tmp_path, *, task_class="standard", **policy_kwargs):
    root = tmp_path / "state"
    _identity(root)
    policy = _policy(root, task_class=task_class, **policy_kwargs)
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(root))
    monkeypatch.setenv("DISH_INVESTIGATION_CALIBRATION", str(policy))
    monkeypatch.setenv("DISH_INVESTIGATION_CLASS", task_class)
    return root


def _state(module, root, host="claude"):
    path = root / "investigation-guard" / "sessions" / host / "1234567890" / "session-1.json"
    return json.loads(path.read_text())


def test_hard_cap_stops_narrow_known_fix_before_runaway(investigation_guard, monkeypatch, tmp_path):
    root = _setup(monkeypatch, tmp_path, cap=2)
    assert investigation_guard.process(_payload("Grep", pattern="a", path="/repo"), "claude") is None
    assert investigation_guard.process(_payload("Read", file_path="/repo/codex/README.md"), "claude") is None
    denied = investigation_guard.process(_payload("Glob", pattern="**/*.md", path="/repo"), "claude")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "CHECKPOINT_REQUIRED" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    state = _state(investigation_guard, root)
    assert state["investigation_count"] == 2
    assert state["checkpoint_required"] is True


def test_material_unknown_targeted_investigation_remains_allowed_inside_cap(investigation_guard, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, cap=3)
    assert investigation_guard.process(_payload("Grep", pattern="responsible_symbol", path="/repo/module.py"), "claude") is None


def test_mandatory_authority_read_is_exempt_once_but_not_renewable(investigation_guard, monkeypatch, tmp_path):
    root = _setup(monkeypatch, tmp_path, cap=2)
    p = _payload("Read", file_path="/repo/CLAUDE.md")
    assert investigation_guard.process(p, "claude") is None
    state = _state(investigation_guard, root)
    assert state["investigation_count"] == 0
    assert investigation_guard.process(p, "claude") is None
    state = _state(investigation_guard, root)
    assert state["investigation_count"] == 1


def test_checkpoint_keeps_finite_focused_proof_tail(investigation_guard, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, cap=1, proof=1)
    assert investigation_guard.process(_payload("Grep", pattern="x", path="/repo"), "claude") is None
    assert investigation_guard.process(_payload("Bash", command="pytest -q hooks/tests/test_investigation_guard.py"), "claude") is None
    denied = investigation_guard.process(_payload("Bash", command="pytest -q hooks/tests/test_investigation_guard.py"), "claude")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "proof tail" in denied["hookSpecificOutput"]["permissionDecisionReason"]


def test_action_ready_early_mode_denies_archaeology_but_allows_action_and_proof(investigation_guard, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, cap=5, proof=2, early=True)
    action = _payload("Edit", file_path="/repo/hooks/investigation-guard", old_string="a", new_string="b")
    assert investigation_guard.process(action, "claude") is None
    denied = investigation_guard.process(_payload("Grep", pattern="history", path="/repo/dish/docs"), "claude")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ACTION_READY" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert investigation_guard.process(_payload("Bash", command="pytest -q hooks/tests/test_investigation_guard.py"), "claude") is None


def test_cap_has_no_broad_read_renewal(investigation_guard, monkeypatch, tmp_path):
    root = _setup(monkeypatch, tmp_path, cap=1)
    assert investigation_guard.process(_payload("Grep", pattern="x", path="/repo"), "claude") is None
    for term in ("y", "z"):
        denied = investigation_guard.process(_payload("Grep", pattern=term, path="/repo"), "claude")
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _state(investigation_guard, root)["investigation_count"] == 1


def test_checkpoint_reason_routes_to_action_or_one_exact_blocker(investigation_guard, monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, cap=1)
    investigation_guard.process(_payload("Grep", pattern="x", path="/repo"), "claude")
    denied = investigation_guard.process(_payload("Grep", pattern="y", path="/repo"), "claude")
    reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert "known action" in reason
    assert "one exact material blocker" in reason
    assert "renewal" in reason


def test_claude_config_intercepts_incident_classes_before_execution(hooks_dir):
    config = json.loads((hooks_dir.parent / ".claude/settings.json").read_text())
    entries = config["hooks"]["PreToolUse"]
    assert len(entries) == 1
    matcher = entries[0]["matcher"]
    for tool in ("Read", "Grep", "Glob", "Bash", "Edit", "Write"):
        assert tool in matcher
    assert "investigation-guard hook --host claude" in entries[0]["hooks"][0]["command"]


def test_codex_hard_scope_is_bash_only_and_other_paths_remain_degraded(hooks_dir):
    config = json.loads((hooks_dir.parent / "codex/hooks.json").read_text())
    entries = config["hooks"]["PreToolUse"]
    bash = next(item for item in entries if item.get("matcher") == "^Bash$")
    commands = [hook["command"] for hook in bash["hooks"]]
    assert "/home/marco/.local/bin/codex-protected-checkout" in commands
    assert "/home/marco/.local/bin/investigation-guard hook --host codex" in commands
    assert not any(item.get("matcher") not in {"^Bash$"} and "investigation-guard" in json.dumps(item) for item in entries)
    readme = (hooks_dir.parent / "codex/README.md").read_text()
    assert "DEGRADED" in readme
    assert "not a process or filesystem sandbox" in readme


def test_unqualified_tool_surface_is_observe_only_never_false_hard_closed(investigation_guard, monkeypatch, tmp_path):
    root = _setup(monkeypatch, tmp_path, cap=1, tools=("Bash",))
    for _ in range(3):
        assert investigation_guard.process(_payload("Grep", pattern="x", path="/repo"), "claude") is None
    state = _state(investigation_guard, root)
    assert "claude:Grep:observe-only" in state["degraded_surfaces"]


def test_missing_calibration_is_observe_only_and_never_denies(investigation_guard, monkeypatch, tmp_path):
    root = tmp_path / "state"
    _identity(root)
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(root))
    monkeypatch.setenv("DISH_INVESTIGATION_CALIBRATION", str(tmp_path / "missing-calibration.json"))
    monkeypatch.setenv("DISH_INVESTIGATION_CLASS", "narrow-fix")
    for term in ("a", "b", "c"):
        assert investigation_guard.process(_payload("Grep", pattern=term, path="/repo"), "claude") is None
    state = _state(investigation_guard, root)
    assert state["investigation_count"] == 3
    assert "claude:Grep:observe-only" in state["degraded_surfaces"]


def test_chatgpt_project_rule_is_explicitly_soft_only(hooks_dir):
    source = json.loads((hooks_dir.parent / "dish/docs/chatgpt-projects/source.json").read_text())
    rules = [r for r in source["shared_rules"] if r.get("id") == "bounded-investigation-termination"]
    assert len(rules) == 1
    assert "DEGRADED_SOFT_ONLY" in rules[0]["text"]
    evals = json.loads((hooks_dir.parent / "dish/docs/chatgpt-projects/evals.json").read_text())
    ids = {scenario["id"] for scenario in evals["scenarios"]}
    assert "action-ready-stops-chatgpt-archaeology" in ids
    assert "chatgpt-manual-never-claims-hard-investigation-closure" in ids


def test_deep_research_requires_separate_calibrated_class_and_gets_larger_envelope(investigation_guard):
    traces = investigation_guard.read_traces(sorted(FIXTURES.glob("*.json")))
    policy = investigation_guard.calibrate(traces)
    narrow = policy["calibrations"]["claude:narrow-fix"]["investigation_cap"]
    deep = policy["calibrations"]["claude:deep-research"]["investigation_cap"]
    assert deep > narrow


def test_calibration_chooses_lowest_observed_safe_cap(investigation_guard):
    traces = investigation_guard.read_traces(sorted(FIXTURES.glob("*.json")))
    policy = investigation_guard.calibrate(traces)
    assert policy["calibrations"]["claude:narrow-fix"]["investigation_cap"] == 2
    assert policy["calibrations"]["codex:narrow-fix"]["investigation_cap"] == 1
    assert policy["calibrations"]["claude:deep-research"]["investigation_cap"] == 5


def test_calibration_refuses_hard_activation_when_no_cap_separates_legit_and_incident(investigation_guard):
    base = {
        "schema_version": 1, "host": "claude", "task_class": "collision", "role": "implementation",
        "intercepted_tools": ["Grep"],
    }
    legit = dict(base, kind="legitimate", events=[
        {"payload": {"tool_name": "Grep", "tool_input": {"pattern": str(i), "path": "/repo"}}, "expected_category": "investigation"}
        for i in range(3)
    ])
    incident = dict(base, kind="incident", stop_before_investigation_count=3, events=[
        {"payload": {"tool_name": "Grep", "tool_input": {"pattern": str(i), "path": "/repo"}}, "expected_category": "investigation"}
        for i in range(5)
    ])
    policy = investigation_guard.calibrate([legit, incident])
    assert "claude:collision" not in policy["calibrations"]


def test_codex_guard_is_silent_outside_ai_tools_scope(investigation_guard, monkeypatch, tmp_path):
    root = _setup(monkeypatch, tmp_path, host="codex", cap=1, tools=("Bash",))
    payload = _payload("Bash", command="grep -R thing .")
    payload["cwd"] = str(tmp_path / "unrelated-repository")
    assert investigation_guard.process(payload, "codex") is None
    assert not (root / "investigation-guard" / "sessions" / "codex").exists()


def test_model_tool_input_cannot_self_promote_to_deep_research(investigation_guard, monkeypatch, tmp_path):
    root = _setup(monkeypatch, tmp_path, task_class="standard", cap=1)
    policy_path = root / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["calibrations"]["claude:deep-research"] = {
        "investigation_cap": 5,
        "proof_tail_cap": 1,
        "qualified_tools": ["Read", "Grep", "Glob", "Bash", "Edit", "Write"],
        "early_action_ready": False,
        "trace_digest": "test-deep",
    }
    policy_path.write_text(json.dumps(policy) + "\n")

    assert investigation_guard.process(_payload("Grep", pattern="first", path="/repo"), "claude") is None
    spoofed = _payload(
        "Grep",
        pattern="second",
        path="/repo",
        env={"DISH_INVESTIGATION_CLASS": "deep-research"},
    )
    denied = investigation_guard.process(spoofed, "claude")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "standard" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert _state(investigation_guard, root)["task_class"] == "standard"


def test_managed_partial_ceiling_is_explicitly_degraded(hooks_dir):
    root_contract = (hooks_dir.parent / "CLAUDE.md").read_text()
    assert "Managed/other runners are HARD only" in root_contract
    assert "partial ceiling remains DEGRADED" in root_contract
