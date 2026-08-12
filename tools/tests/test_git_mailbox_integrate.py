import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "git-mailbox-integrate.py"
COMMIT_TOOL = Path(__file__).resolve().parent.parent / "git-commit"
GIT = shutil.which("git")


def git(repo, *args, check=True):
    return subprocess.run([GIT, "-C", str(repo), *args], check=check, text=True, capture_output=True)


def run(worktree, branch, *mailboxes, execution_cwd=None, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--worktree", str(worktree), "--branch", branch,
         "-C", str(execution_cwd or worktree), *(str(p) for p in mailboxes)],
        cwd=worktree.parent,
        env=env,
        text=True,
        capture_output=True,
    )


def install_commit_tool(worktree):
    tools = worktree / "tools"
    tools.mkdir(exist_ok=True)
    shutil.copy2(COMMIT_TOOL, tools / "git-commit")
    (tools / "git-commit").chmod(0o755)
    git(worktree, "add", "tools/git-commit")
    git(worktree, "commit", "-q", "-m", "install commit tool")


def make_series(
    tmp_path,
    *,
    two=False,
    conflicting_second=False,
    first_change="file",
):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.name", "tester")
    git(main, "config", "user.email", "tester@example.com")
    git(main, "checkout", "-q", "-b", "main")
    (main / "base.txt").write_text("base\n")
    (main / "type-target.txt").write_text("target\n")
    (main / "type-change").write_text("regular\n")
    git(main, "add", "base.txt", "type-target.txt", "type-change")
    git(main, "commit", "-q", "-m", "base")
    install_commit_tool(main)
    base = git(main, "rev-parse", "HEAD").stdout.strip()

    integration = tmp_path / "integration"
    git(main, "worktree", "add", "-q", "-b", "integration", str(integration), base)

    author = tmp_path / "author"
    subprocess.run([GIT, "clone", "-q", str(main), str(author)], check=True)
    git(author, "config", "user.name", "Patch Author")
    git(author, "config", "user.email", "author@example.com")
    git(author, "checkout", "-q", "-b", "candidate", base)

    if first_change == "file":
        (author / "one.txt").write_text("one\n")
    elif first_change == "leading-dash":
        (author / "-m").write_text("option-looking filename\n")
    elif first_change == "commit-tool":
        malicious = author / "tools" / "git-commit"
        malicious.write_text(
            "#!/bin/sh\n"
            "git commit -m malicious\n"
            "git update-ref refs/heads/main HEAD\n"
        )
        malicious.chmod(0o755)
    elif first_change == "type-change":
        path = author / "type-change"
        path.unlink()
        path.symlink_to("type-target.txt")
    else:
        raise AssertionError(first_change)

    git(author, "add", "-A")
    git(author, "commit", "-q", "-m", "candidate one")

    if two:
        if conflicting_second:
            (author / "base.txt").write_text("candidate conflict\n")
        else:
            (author / "two.txt").write_text("two\n")
        git(author, "add", "-A")
        git(author, "commit", "-q", "-m", "candidate two")

    mailbox = tmp_path / "series.mbox"
    with mailbox.open("w") as out:
        subprocess.run(
            [GIT, "-C", str(author), "format-patch", "--stdout", f"{base}..HEAD"],
            check=True,
            text=True,
            stdout=out,
        )
    return main, integration, author, mailbox, base


def heads(main, integration):
    return (
        git(main, "rev-parse", "refs/heads/main").stdout.strip(),
        git(integration, "rev-parse", "HEAD").stdout.strip(),
    )


def test_successful_mailbox_application_and_main_unchanged(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path, two=True)
    main_before, integration_before = heads(main, integration)
    result = run(integration, "integration", mailbox)
    assert result.returncode == 0, result.stderr
    main_after, integration_after = heads(main, integration)
    assert main_after == main_before
    assert integration_after != integration_before
    assert (integration / "one.txt").read_text() == "one\n"
    assert (integration / "two.txt").read_text() == "two\n"
    authors = git(integration, "log", "-2", "--format=%an <%ae>").stdout.splitlines()
    assert authors == ["Patch Author <author@example.com>", "Patch Author <author@example.com>"]


def test_partial_mailbox_failure_preserves_prefix_and_main(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path, two=True, conflicting_second=True)
    # Create an integration-side change to the same line *before* the series;
    # then export series from the earlier base. Worktree must still be clean.
    (integration / "base.txt").write_text("integration conflict\n")
    git(integration, "add", "base.txt")
    git(integration, "commit", "-q", "-m", "integration divergence")
    main_before = git(main, "rev-parse", "refs/heads/main").stdout.strip()
    before_count = int(git(integration, "rev-list", "--count", "HEAD").stdout)
    result = run(integration, "integration", mailbox)
    assert result.returncode == 1
    assert "failed to apply" in result.stderr
    after_count = int(git(integration, "rev-list", "--count", "HEAD").stdout)
    assert after_count == before_count + 1
    assert (integration / "one.txt").exists()
    assert git(integration, "status", "--porcelain").stdout == ""
    assert git(main, "rev-parse", "refs/heads/main").stdout.strip() == main_before


def test_wrong_dash_C_directory_fails_before_mutation(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path)
    main_before, integration_before = heads(main, integration)
    result = run(integration, "integration", mailbox, execution_cwd=main)
    assert result.returncode == 1
    assert "wrong worktree" in result.stderr
    assert heads(main, integration) == (main_before, integration_before)


@pytest.mark.parametrize("name,value_from", [("GIT_DIR", "gitdir"), ("GIT_WORK_TREE", "main")])
def test_repository_resolution_overrides_fail_closed(tmp_path, name, value_from):
    main, integration, _, mailbox, _ = make_series(tmp_path)
    main_before, integration_before = heads(main, integration)
    env = os.environ.copy()
    env[name] = git(main, "rev-parse", "--absolute-git-dir").stdout.strip() if value_from == "gitdir" else str(main)
    result = run(integration, "integration", mailbox, env=env)
    assert result.returncode == 1
    assert name in result.stderr
    assert heads(main, integration) == (main_before, integration_before)


def test_git_config_environment_injection_fails_closed(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path)
    main_before, integration_before = heads(main, integration)
    env = os.environ.copy()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    env["GIT_CONFIG_VALUE_0"] = str(integration / "hooks")
    result = run(integration, "integration", mailbox, env=env)
    assert result.returncode == 1
    assert "GIT_CONFIG_COUNT" in result.stderr
    assert heads(main, integration) == (main_before, integration_before)


def test_wrong_branch_worktree_identity_fails(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path)
    main_before, integration_before = heads(main, integration)
    result = run(integration, "not-integration", mailbox)
    assert result.returncode == 1
    assert "wrong branch/worktree identity" in result.stderr
    assert heads(main, integration) == (main_before, integration_before)


def test_main_branch_is_never_a_mailbox_candidate(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path)
    main_before, integration_before = heads(main, integration)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--worktree", str(main), "--branch", "main", "-C", str(main), str(mailbox)],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "refuses refs/heads/main" in result.stderr
    assert heads(main, integration) == (main_before, integration_before)


def test_candidate_replacement_of_commit_tool_is_not_executed(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path, first_change="commit-tool")
    main_before, integration_before = heads(main, integration)
    result = run(integration, "integration", mailbox)
    assert result.returncode == 0, result.stderr
    main_after, integration_after = heads(main, integration)
    assert main_after == main_before
    assert integration_after != integration_before
    assert "update-ref refs/heads/main" in (integration / "tools" / "git-commit").read_text()
    assert git(integration, "log", "-1", "--format=%s").stdout.strip() == "candidate one"


def test_active_post_commit_hook_is_neutralized_and_main_unchanged(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path)
    main_before = git(main, "rev-parse", "refs/heads/main").stdout.strip()
    marker = tmp_path / "hook-ran"
    hooks = tmp_path / "candidate-hooks"
    hooks.mkdir()
    hook = hooks / "post-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"touch {marker}\n"
        "git update-ref refs/heads/main HEAD\n"
    )
    hook.chmod(0o755)
    git(integration, "config", "core.hooksPath", str(hooks))

    result = run(integration, "integration", mailbox)
    assert result.returncode == 0, result.stderr
    assert git(main, "rev-parse", "refs/heads/main").stdout.strip() == main_before
    assert not marker.exists()
    assert (integration / "one.txt").read_text() == "one\n"


def test_local_fsmonitor_is_neutralized_before_any_repository_git(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path)
    # Diverge the integration branch so a malicious fsmonitor that updates main
    # to integration HEAD would produce an observable protected-ref mutation.
    (integration / "integration-only.txt").write_text("integration\n")
    git(integration, "add", "integration-only.txt")
    git(integration, "commit", "-q", "-m", "integration divergence")

    main_before = git(main, "rev-parse", "refs/heads/main").stdout.strip()
    marker = tmp_path / "fsmonitor-ran"
    fsmonitor = tmp_path / "malicious-fsmonitor"
    fsmonitor.write_text(
        "#!/bin/sh\n"
        f"touch {marker}\n"
        f"git -C {integration} update-ref refs/heads/main HEAD\n"
        "exit 0\n"
    )
    fsmonitor.chmod(0o755)
    git(integration, "config", "core.fsmonitor", str(fsmonitor))

    result = run(integration, "integration", mailbox)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert git(main, "rev-parse", "refs/heads/main").stdout.strip() == main_before
    assert (integration / "one.txt").read_text() == "one\n"


def test_leading_dash_filename_is_committed_as_data(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path, first_change="leading-dash")
    main_before = git(main, "rev-parse", "refs/heads/main").stdout.strip()
    result = run(integration, "integration", mailbox)
    assert result.returncode == 0, result.stderr
    assert (integration / "-m").read_text() == "option-looking filename\n"
    assert git(integration, "status", "--porcelain").stdout == ""
    assert git(main, "rev-parse", "refs/heads/main").stdout.strip() == main_before


def test_type_change_only_patch_is_committed(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path, first_change="type-change")
    main_before = git(main, "rev-parse", "refs/heads/main").stdout.strip()
    result = run(integration, "integration", mailbox)
    assert result.returncode == 0, result.stderr
    path = integration / "type-change"
    assert path.is_symlink()
    assert os.readlink(path) == "type-target.txt"
    assert git(integration, "diff", "HEAD^", "HEAD", "--summary").stdout.strip().startswith("mode change 100644 => 120000 type-change")
    assert git(main, "rev-parse", "refs/heads/main").stdout.strip() == main_before
