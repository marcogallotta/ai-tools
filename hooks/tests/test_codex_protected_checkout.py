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
            "cwd": str(protected_repo["primary"]),
            "tool_input": {
                "command": "git switch agent/fail",
                "workdir": str(protected_repo["primary"]),
            },
        },
        monkeypatch,
        capsys,
    )
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_adapter_is_silent_outside_ai_tools_even_for_ai_tools_workdir(
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
    assert decision is None


def test_codex_hooks_config_is_user_level_and_hard_deny_adapter(hooks_dir):
    config = json.loads((hooks_dir.parent / "codex" / "hooks.json").read_text())
    entries = config["hooks"]["PreToolUse"]
    entry = next(item for item in entries if item.get("matcher") == "^Bash$")
    assert entry["hooks"][0]["command"] == "/home/marco/.local/bin/codex-protected-checkout"
    permission = config["hooks"]["PermissionRequest"][0]
    assert permission["hooks"][0]["command"] == "/home/marco/.local/bin/codex-protected-checkout"
    rules = (hooks_dir.parent / "codex" / "git-pr.rules").read_text()
    assert 'decision="prompt"' in rules
    assert '["gh", "pr"]' in rules


def test_codex_permission_request_allows_feature_and_pr_but_not_main(
    codex_protected_checkout, protected_repo, monkeypatch, capsys
):
    for command, cwd, allowed in (
        ("git reset --hard HEAD", protected_repo["linked"], True),
        ("git push origin HEAD:main", protected_repo["linked"], False),
        ("git commit -m x", protected_repo["primary"], False),
        ("gh pr merge 42", protected_repo["primary"], True),
        ("git status; git commit -m x", protected_repo["linked"], True),
    ):
        decision = run_adapter(codex_protected_checkout, {
            "hook_event_name": "PermissionRequest", "tool_name": "Bash",
            "cwd": str(cwd), "tool_input": {"command": command},
        }, monkeypatch, capsys)
        assert (decision is not None) is allowed
        if allowed:
            assert decision["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


def test_codex_permission_request_allows_reported_workflows(
    codex_protected_checkout, protected_repo, monkeypatch, capsys
):
    linked = str(protected_repo["linked"])
    commands = (
        f"git -C {linked} status --short && git -C {linked} branch --show-current",
        'git status; echo "---UPSTREAM---"; git rev-parse --abbrev-ref --symbolic-full-name @{upstream} 2>&1',
        'git fetch origin pull/30/head:pr30 2>&1; pwd; git branch -a | grep -E "pr30"',
        "git log --oneline -20 && echo --- && git status && echo --- && git branch -vv && echo --- && git remote -v",
    )
    for command in commands:
        decision = run_adapter(codex_protected_checkout, {
            "hook_event_name": "PermissionRequest", "tool_name": "Bash",
            "cwd": linked, "tool_input": {"command": command},
        }, monkeypatch, capsys)
        assert decision["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    for command in (
        "git status; rm -rf relative-path",
        "git status; ssh host uname",
        "git -c alias.x='!echo x' x",
    ):
        decision = run_adapter(codex_protected_checkout, {
            "hook_event_name": "PermissionRequest", "tool_name": "Bash",
            "cwd": linked, "tool_input": {"command": command},
        }, monkeypatch, capsys)
        assert decision is None


def test_codex_adapter_denies_raw_commit_in_nested_active_worktree(
    codex_protected_checkout, protected_repo, monkeypatch, capsys, tmp_path
):
    import subprocess

    linked = protected_repo["linked"].resolve()
    home = tmp_path / "home"
    state_root = home / ".local/state/dish/worktrees/12345"
    state_root.mkdir(parents=True)
    git_dir = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--path-format=absolute", "--git-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    common_dir = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (state_root / ("a" * 24 + "-" + "b" * 32 + ".json")).write_text(json.dumps({
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
    for command in ("git commit -m x", "git status; git add README.md"):
        decision = run_adapter(
            codex_protected_checkout,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "cwd": str(linked),
                "tool_input": {"command": command},
            },
            monkeypatch,
            capsys,
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "agent-worktree" in decision["hookSpecificOutput"]["permissionDecisionReason"]
