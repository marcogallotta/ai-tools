import io
import json


def run_adapter(module, payload, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert module.main() == 0
    output = capsys.readouterr().out
    return json.loads(output) if output.strip() else None


def test_codex_adapter_emits_supported_hard_deny(
    codex_protected_checkout, protected_repo, monkeypatch, capsys
):
    monkeypatch.setattr(
        codex_protected_checkout.protected_checkout,
        "DEFAULT_PROTECTED_CHECKOUT_ROOT",
        str(protected_repo["primary"]),
    )
    decision = run_adapter(
        codex_protected_checkout,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(protected_repo["primary"]),
            "tool_input": {"command": "git switch -c agent/incident"},
        },
        monkeypatch,
        capsys,
    )
    output = decision["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert "permissionDecisionReason" in output
    assert "ask" not in json.dumps(decision)


def test_codex_adapter_stays_silent_for_unrelated_tool(
    codex_protected_checkout, monkeypatch, capsys
):
    decision = run_adapter(
        codex_protected_checkout,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "cwd": "/tmp",
            "tool_input": {"command": "git switch main"},
        },
        monkeypatch,
        capsys,
    )
    assert decision is None


def test_codex_adapter_honors_per_call_workdir(
    codex_protected_checkout, protected_repo, monkeypatch, capsys
):
    monkeypatch.setattr(
        codex_protected_checkout.protected_checkout,
        "DEFAULT_PROTECTED_CHECKOUT_ROOT",
        str(protected_repo["primary"]),
    )
    decision = run_adapter(
        codex_protected_checkout,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(protected_repo["unrelated"]),
            "tool_input": {
                "command": "git switch agent/fail",
                "workdir": str(protected_repo["primary"]),
            },
        },
        monkeypatch,
        capsys,
    )
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_hooks_config_is_user_level_and_hard_deny_adapter(hooks_dir):
    config = json.loads((hooks_dir.parent / "codex" / "hooks.json").read_text())
    entries = config["hooks"]["PreToolUse"]
    entry = next(item for item in entries if item.get("matcher") == "^Bash$")
    assert entry["hooks"][0]["command"] == "/home/marco/.local/bin/codex-protected-checkout"


def test_codex_adapter_denies_branch_change_in_registered_active_task_worktree(
    codex_protected_checkout, protected_repo, monkeypatch, capsys, tmp_path
):
    import subprocess

    linked = protected_repo["linked"].resolve()
    home = tmp_path / "home"
    state_root = home / ".local/state/dish/worktrees"
    state_root.mkdir(parents=True)
    git_dir = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--path-format=absolute", "--git-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    common_dir = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (state_root / "12345.json").write_text(json.dumps({
        "task_gid": "12345",
        "branch": "agent/existing",
        "worktree_path": str(linked),
        "git_dir": git_dir,
        "git_common_dir": common_dir,
        "lifecycle": "active",
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        codex_protected_checkout.protected_checkout,
        "DEFAULT_PROTECTED_CHECKOUT_ROOT",
        str(protected_repo["primary"]),
    )
    decision = run_adapter(
        codex_protected_checkout,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(linked),
            "tool_input": {"command": "git switch main"},
        },
        monkeypatch,
        capsys,
    )
    output = decision["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "12345" in output["permissionDecisionReason"]
    assert "task-owned branch is fixed" in output["permissionDecisionReason"]
