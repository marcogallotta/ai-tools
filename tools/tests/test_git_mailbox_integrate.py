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


def make_series(tmp_path, *, two=False, conflicting_second=False):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.name", "tester")
    git(main, "config", "user.email", "tester@example.com")
    git(main, "checkout", "-q", "-b", "main")
    (main / "base.txt").write_text("base\n")
    git(main, "add", "base.txt")
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
    (author / "one.txt").write_text("one\n")
    git(author, "add", "one.txt")
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
        subprocess.run([GIT, "-C", str(author), "format-patch", "--stdout", f"{base}..HEAD"], check=True, text=True, stdout=out)
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
        text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert "refuses refs/heads/main" in result.stderr
    assert heads(main, integration) == (main_before, integration_before)


def test_detects_main_movement_during_commit(tmp_path):
    main, integration, _, mailbox, _ = make_series(tmp_path)
    main_before = git(main, "rev-parse", "refs/heads/main").stdout.strip()
    # A hook models an unexpected tool/hook side effect during the guarded
    # commit. The wrapper must stop as soon as the commit returns and sees main
    # moved; it must not continue to another mailbox patch.
    hook = Path(git(integration, "rev-parse", "--git-path", "hooks/post-commit").stdout.strip())
    if not hook.is_absolute():
        hook = integration / hook
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        "git update-ref refs/heads/main HEAD\n"
    )
    hook.chmod(0o755)
    result = run(integration, "integration", mailbox)
    assert result.returncode == 1
    assert "refs/heads/main moved unexpectedly" in result.stderr
    assert git(main, "rev-parse", "refs/heads/main").stdout.strip() != main_before
