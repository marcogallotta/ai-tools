import subprocess

import pytest


@pytest.mark.parametrize("command", [
    "git commit -m x", "git push --force", "git reset --hard HEAD",
    "git clean -fd", "git restore README.md", "git branch -D old",
    "git branch -uorigin/main",
])
def test_prompt_free_git_accepts_ordinary_feature_mutations(
    classifier_module, protected_repo, command
):
    assert classifier_module.prompt_free_git(command, protected_repo["linked"])


@pytest.mark.parametrize("command,cwd", [
    ("git commit -m x", "primary"),
    ("git push origin HEAD:main", "linked"),
    ("git push origin +main", "linked"),
    ("git branch -D main", "linked"),
    ("git branch --set-upstream-toorigin/main", "primary"),
    ("git push --all", "linked"),
    ("git push --mirror", "linked"),
    ('git push origin "$DEST"', "linked"),
    ('git branch -D "$TARGET"', "linked"),
    ("git status; git commit -m x", "linked"),
    ("env git commit -m x", "linked"),
])
def test_prompt_free_git_rejects_main_and_unclear_forms(
    classifier_module, protected_repo, command, cwd
):
    assert not classifier_module.prompt_free_git(command, protected_repo[cwd])


def test_prompt_free_git_accepts_direct_pr_command(classifier_module, protected_repo):
    assert classifier_module.prompt_free_git("gh pr merge 42", protected_repo["primary"])


@pytest.mark.parametrize("command", [
    "git -C /tmp status --short && git -C /tmp branch --show-current",
    'git status; echo "---UPSTREAM---"; git rev-parse --abbrev-ref --symbolic-full-name @{upstream} 2>&1',
    'git fetch origin pull/30/head:pr30 2>&1; pwd; git branch -a | grep -E "pr30"',
    "git log --oneline -20 && echo --- && git status && echo --- && git branch -vv && echo --- && git remote -v",
    "git status; git commit -m routine-feature-commit",
    "git branch --unset-upstream",
])
def test_prompt_free_workflow_accepts_routine_compounds(
    classifier_module, protected_repo, command
):
    assert classifier_module.prompt_free_workflow(command, protected_repo["linked"])


@pytest.mark.parametrize("command", [
    "git fetch origin; rm -rf /tmp/no",
    'bash -c "echo x" git status',
    'git show "$(touch /tmp/no)"',
    "git status > /tmp/no",
    "cd /tmp > /tmp/no && git status",
    "cd /tmp $(touch /tmp/no) && git status",
])
def test_prompt_free_workflow_rejects_unsafe_compounds(classifier_module, command):
    assert not classifier_module.prompt_free_workflow(command)


def classify(module, protected_repo, command, cwd="primary"):
    return module.classify(
        command,
        str(protected_repo[cwd]),
        protected_root=str(protected_repo["primary"]),
    )


def test_direct_and_dash_c_target_primary(classifier_module, protected_repo):
    assert "Refusing 'switch'" in classify(classifier_module, protected_repo, "git switch main")
    command = f"git -C {protected_repo['primary']} checkout -b agent/fail"
    assert "Refusing 'checkout'" in classify(
        classifier_module, protected_repo, command, cwd="unrelated"
    )


def test_attached_and_flag_only_checkout_mutations_are_denied(
    classifier_module, protected_repo
):
    commands = (
        "git checkout -bnew-branch",
        "git checkout -Breset-branch",
        "git checkout -qbnew-branch",
        "git checkout --orphan=orphan-branch",
        "git checkout --orphan orphan-branch",
        "git checkout --detach",
        "git checkout -d",
        "git checkout -qd",
    )
    for command in commands:
        assert classify(classifier_module, protected_repo, command) is not None


def test_checkout_option_forms_remain_allowed_outside_primary(
    classifier_module, protected_repo
):
    commands = (
        "git checkout -bnew-branch",
        "git checkout --orphan=orphan-branch",
        "git checkout --detach",
    )
    for command in commands:
        assert classify(classifier_module, protected_repo, command, cwd="linked") is None
        assert classify(classifier_module, protected_repo, command, cwd="unrelated") is None


def test_nested_shell_visible_git_is_denied(classifier_module, protected_repo):
    reason = classify(
        classifier_module, protected_repo, "bash -lc 'git switch -c agent/fail'"
    )
    assert "Refusing 'switch'" in reason


def test_visible_cd_then_nested_git_is_denied(classifier_module, protected_repo):
    command = f"bash -lc 'cd {protected_repo['primary']} && git switch agent/fail'"
    reason = classify(classifier_module, protected_repo, command, cwd="unrelated")
    assert "Refusing 'switch'" in reason


def test_visible_cd_then_persistent_shell_is_denied(classifier_module, protected_repo):
    command = f"cd {protected_repo['primary']} && bash"
    reason = classify(classifier_module, protected_repo, command, cwd="unrelated")
    assert "write_stdin" in reason


def test_shell_control_prefix_does_not_hide_git(classifier_module, protected_repo):
    reason = classify(classifier_module, protected_repo, "then git switch agent/fail")
    assert "Refusing 'switch'" in reason


def test_persistent_shell_in_primary_is_denied(classifier_module, protected_repo):
    reason = classify(classifier_module, protected_repo, "bash -il")
    assert "write_stdin" in reason


def test_persistent_shell_in_linked_and_unrelated_repos_is_allowed(
    classifier_module, protected_repo
):
    assert classify(classifier_module, protected_repo, "bash", cwd="linked") is None
    assert classify(classifier_module, protected_repo, "zsh -l", cwd="unrelated") is None


def test_git_config_alias_to_checkout_is_denied(classifier_module, protected_repo):
    reason = classify(
        classifier_module,
        protected_repo,
        "git -c alias.start='checkout -b agent/fail' start",
    )
    assert "Refusing 'checkout'" in reason


def test_repository_alias_to_switch_is_denied(classifier_module, protected_repo):
    subprocess.run(
        ["git", "-C", str(protected_repo["primary"]), "config", "alias.move", "switch main"],
        check=True,
    )
    assert "Refusing 'switch'" in classify(classifier_module, protected_repo, "git move")


def test_visible_shell_git_alias_is_denied(classifier_module, protected_repo):
    reason = classify(
        classifier_module,
        protected_repo,
        "git -c alias.move='!git switch main' move",
    )
    assert "Refusing 'switch'" in reason


def test_shell_expanded_command_line_alias_fails_closed(classifier_module, protected_repo):
    reason = classify(
        classifier_module,
        protected_repo,
        'git -c alias.move="$MUTATOR" move',
    )
    assert "cannot be resolved safely" in reason


def test_unrelated_shell_expanded_environment_does_not_block_readonly_git(
    classifier_module, protected_repo
):
    assert classify(classifier_module, protected_repo, "UNRELATED=$VALUE git status") is None


def test_shell_expanded_alias_in_unrelated_repo_retains_ordinary_behavior(
    classifier_module, protected_repo
):
    assert (
        classify(
            classifier_module,
            protected_repo,
            'git -c alias.move="$MUTATOR" move',
            cwd="unrelated",
        )
        is None
    )


def test_config_env_alias_uses_visible_command_environment(classifier_module, protected_repo):
    commands = (
        "MUTATOR='checkout -b agent/fail' git --config-env=alias.move=MUTATOR move",
        "MUTATOR='checkout -b agent/fail' git --config-env alias.move=MUTATOR move",
    )
    for command in commands:
        assert "Refusing 'checkout'" in classify(classifier_module, protected_repo, command)


def test_git_config_count_alias_uses_visible_command_environment(
    classifier_module, protected_repo
):
    command = (
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.move "
        "GIT_CONFIG_VALUE_0='checkout -b agent/fail' git move"
    )
    assert "Refusing 'checkout'" in classify(classifier_module, protected_repo, command)


def test_config_env_is_preserved_through_recursive_alias_expansion(
    classifier_module, protected_repo
):
    subprocess.run(
        [
            "git",
            "-C",
            str(protected_repo["primary"]),
            "config",
            "alias.second",
            "checkout -b chained-config-env",
        ],
        check=True,
    )
    command = "MUTATOR=second git --config-env=alias.first=MUTATOR first"
    assert "Refusing 'checkout'" in classify(classifier_module, protected_repo, command)


def test_git_config_count_is_preserved_through_recursive_alias_expansion(
    classifier_module, protected_repo
):
    command = (
        "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=alias.first GIT_CONFIG_VALUE_0=second "
        "GIT_CONFIG_KEY_1=alias.second "
        "GIT_CONFIG_VALUE_1='checkout -b chained-count' git first"
    )
    assert "Refusing 'checkout'" in classify(classifier_module, protected_repo, command)


def test_recursive_command_environment_alias_forms_remain_allowed_outside_primary(
    classifier_module, protected_repo
):
    for location in ("linked", "unrelated"):
        subprocess.run(
            [
                "git",
                "-C",
                str(protected_repo[location]),
                "config",
                "alias.second",
                "checkout -b chained-config-env",
            ],
            check=True,
        )
    commands = (
        "MUTATOR=second git --config-env=alias.first=MUTATOR first",
        "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=alias.first GIT_CONFIG_VALUE_0=second "
        "GIT_CONFIG_KEY_1=alias.second "
        "GIT_CONFIG_VALUE_1='checkout -b chained-count' git first",
    )
    for command in commands:
        assert classify(classifier_module, protected_repo, command, cwd="linked") is None
        assert classify(classifier_module, protected_repo, command, cwd="unrelated") is None


def test_config_env_is_preserved_through_shell_alias_expansion(
    classifier_module, protected_repo
):
    subprocess.run(
        [
            "git",
            "-C",
            str(protected_repo["primary"]),
            "config",
            "alias.first",
            "!git second",
        ],
        check=True,
    )
    commands = (
        "MUTATOR='checkout -b shell-chain' "
        "git --config-env=alias.second=MUTATOR first",
        "git -c alias.second='checkout -b shell-chain' first",
    )
    for command in commands:
        assert "Refusing 'checkout'" in classify(classifier_module, protected_repo, command)


def test_invocation_config_shell_alias_remains_allowed_outside_primary(
    classifier_module, protected_repo
):
    commands = (
        "MUTATOR='checkout -b shell-chain' "
        "git --config-env=alias.second=MUTATOR first",
        "git -c alias.second='checkout -b shell-chain' first",
    )
    for location in ("linked", "unrelated"):
        subprocess.run(
            [
                "git",
                "-C",
                str(protected_repo[location]),
                "config",
                "alias.first",
                "!git second",
            ],
            check=True,
        )
        for command in commands:
            assert classify(classifier_module, protected_repo, command, cwd=location) is None


def test_command_environment_alias_forms_remain_allowed_outside_primary(
    classifier_module, protected_repo
):
    commands = (
        "MUTATOR='checkout -b agent/fail' git --config-env=alias.move=MUTATOR move",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.move "
        "GIT_CONFIG_VALUE_0='checkout -b agent/fail' git move",
    )
    for command in commands:
        assert classify(classifier_module, protected_repo, command, cwd="linked") is None
        assert classify(classifier_module, protected_repo, command, cwd="unrelated") is None


def test_linked_worktree_and_unrelated_repo_branch_changes_are_allowed(
    classifier_module, protected_repo
):
    assert classify(classifier_module, protected_repo, "git switch main", cwd="linked") is None
    assert classify(classifier_module, protected_repo, "git checkout main", cwd="unrelated") is None


def test_opaque_python_subprocess_is_explicitly_outside_classifier(
    classifier_module, protected_repo
):
    assert classify(classifier_module, protected_repo, "python3 opaque.py") is None


def _register_active_linked_worktree(protected_repo, monkeypatch, tmp_path, task_gid="12345"):
    import json
    import os

    home = tmp_path / "home"
    state_root = home / ".local/state/dish/worktrees"
    state_root.mkdir(parents=True)
    linked = protected_repo["linked"].resolve()
    git_dir = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--path-format=absolute", "--git-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    common_dir = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (state_root / f"{task_gid}.json").write_text(json.dumps({
        "task_gid": task_gid,
        "branch": "agent/existing",
        "worktree_path": str(linked),
        "git_dir": git_dir,
        "git_common_dir": common_dir,
        "lifecycle": "active",
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return task_gid


def test_registered_active_task_worktree_branch_change_is_denied(
    classifier_module, protected_repo, monkeypatch, tmp_path
):
    task_gid = _register_active_linked_worktree(protected_repo, monkeypatch, tmp_path)
    reason = classify(classifier_module, protected_repo, "git switch main", cwd="linked")
    assert task_gid in reason
    assert "task-owned branch is fixed" in reason


def test_unregistered_linked_worktree_branch_change_retains_existing_behavior(
    classifier_module, protected_repo
):
    assert classify(classifier_module, protected_repo, "git switch main", cwd="linked") is None
