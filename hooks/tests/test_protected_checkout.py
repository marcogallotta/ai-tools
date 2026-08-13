import subprocess


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


def test_linked_worktree_and_unrelated_repo_branch_changes_are_allowed(
    classifier_module, protected_repo
):
    assert classify(classifier_module, protected_repo, "git switch main", cwd="linked") is None
    assert classify(classifier_module, protected_repo, "git checkout main", cwd="unrelated") is None


def test_opaque_python_subprocess_is_explicitly_outside_classifier(
    classifier_module, protected_repo
):
    assert classify(classifier_module, protected_repo, "python3 opaque.py") is None
