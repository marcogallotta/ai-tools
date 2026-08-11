"""Black-box tests for tools/git-commit.

Runs the real script as a subprocess against throwaway git repos in tmp_path,
rather than importing it, since its behavior is defined by what it does to a
real git index/working tree -- staging, pushing, the DISH_VERSION guard --
not by any internal function contract.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "git-commit"
REAL_GIT = shutil.which("git")


def run(repo, *args, env=None):
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        env=env,
    )


def init_repo(repo):
    subprocess.run([REAL_GIT, "init", "-q"], cwd=repo, check=True)
    subprocess.run([REAL_GIT, "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run([REAL_GIT, "config", "user.name", "test"], cwd=repo, check=True)


def log_subjects(repo):
    result = subprocess.run(
        [REAL_GIT, "log", "--format=%s"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.splitlines()


@pytest.fixture
def repo(tmp_path):
    init_repo(tmp_path)
    return tmp_path


def seed_origin(repo, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run([REAL_GIT, "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run([REAL_GIT, "checkout", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([REAL_GIT, "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run([REAL_GIT, "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run([REAL_GIT, "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    subprocess.run([REAL_GIT, "push", "-q", "origin", "main"], cwd=repo, check=True)
    return remote


@pytest.fixture
def push_shim(tmp_path):
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys

mode = os.environ["PUSH_SHIM_MODE"]
state = os.environ["PUSH_SHIM_STATE"]
real_git = os.environ["REAL_GIT"]
command = sys.argv[3] if len(sys.argv) > 3 and sys.argv[1] == "-C" else sys.argv[1]

if command == "push":
    try:
        count = int(open(state).read()) + 1
    except FileNotFoundError:
        count = 1
    with open(state, "w") as handle:
        handle.write(str(count))
    if mode == "retry" and count >= 3:
        os.execv(real_git, [real_git, *sys.argv[1:]])
    if mode == "present" and count == 1:
        subprocess.run([real_git, *sys.argv[1:]], check=True, capture_output=True)
    print(f"simulated push failure {count}", file=sys.stderr)
    sys.exit(1)

if command == "fetch" and mode == "unknown":
    print("simulated fetch failure", file=sys.stderr)
    sys.exit(1)

os.execv(real_git, [real_git, *sys.argv[1:]])
"""
    )
    shim.chmod(0o755)

    def environment(mode):
        env = os.environ.copy()
        env["PATH"] = f"{shim_dir}:{env['PATH']}"
        env["PUSH_SHIM_MODE"] = mode
        env["PUSH_SHIM_STATE"] = str(tmp_path / f"{mode}.count")
        env["REAL_GIT"] = REAL_GIT
        return env

    return environment


def test_commits_named_file(repo):
    (repo / "foo.txt").write_text("hi\n")
    result = run(repo, "foo.txt", "-m", "add foo")
    assert result.returncode == 0, result.stderr
    assert log_subjects(repo) == ["add foo"]


def test_does_not_stage_unnamed_files(repo):
    (repo / "foo.txt").write_text("hi\n")
    (repo / "bar.txt").write_text("bye\n")
    result = run(repo, "foo.txt", "-m", "add foo only")
    assert result.returncode == 0, result.stderr
    status = subprocess.run(
        ["git", "status", "--short"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout
    assert "bar.txt" in status  # still untracked


def test_refuses_bare_dot(repo):
    (repo / "foo.txt").write_text("hi\n")
    result = run(repo, ".", "-m", "x")
    assert result.returncode == 1
    assert "not allowed" in result.stderr


@pytest.mark.parametrize("pattern", ["-A", "-u", "--all"])
def test_dash_prefixed_carpet_bomb_flags_are_rejected_as_unknown_flags(repo, pattern):
    # These look like flags to the parser, so they're caught by the generic
    # "unknown flag" check before ever reaching the carpet-bomb file-pattern
    # check below -- same behavior as the original bash parser's case
    # statement, which tests these against -*) before the file-list default.
    (repo / "foo.txt").write_text("hi\n")
    result = run(repo, pattern, "-m", "x")
    assert result.returncode == 1
    assert "Unknown flag" in result.stderr


@pytest.mark.parametrize("pattern", ["-A", "-u", "--all"])
def test_dash_prefixed_carpet_bomb_flags_after_separator_hit_the_real_guard(repo, pattern):
    # Passed after "--", they bypass flag parsing and land in the file list,
    # where the explicit carpet-bomb-pattern check catches them.
    (repo / "foo.txt").write_text("hi\n")
    result = run(repo, "-m", "x", "--", pattern)
    assert result.returncode == 1
    assert "not allowed" in result.stderr


def test_requires_message(repo):
    (repo / "foo.txt").write_text("hi\n")
    result = run(repo, "foo.txt")
    assert result.returncode == 1
    assert "-m <message> is required" in result.stderr


def test_requires_at_least_one_file(repo):
    result = run(repo, "-m", "x")
    assert result.returncode == 1
    assert "at least one explicit file path" in result.stderr


def test_rejects_unknown_flag(repo):
    (repo / "foo.txt").write_text("hi\n")
    result = run(repo, "foo.txt", "-m", "x", "--bogus")
    assert result.returncode == 1
    assert "Unknown flag" in result.stderr


def test_missing_file_reports_diagnostic(repo):
    result = run(repo, "nope.txt", "-m", "x")
    assert result.returncode == 1
    assert "did not resolve to anything" in result.stderr
    assert "nope.txt" in result.stderr


def test_rejects_amend(repo):
    (repo / "foo.txt").write_text("hi\n")
    result = run(repo, "foo.txt", "--amend", "-m", "no")
    assert result.returncode == 1
    assert "Unknown flag: --amend" in result.stderr


def test_stages_and_commits_a_plain_deletion(repo):
    (repo / "foo.txt").write_text("hi\n")
    run(repo, "foo.txt", "-m", "add foo")
    (repo / "foo.txt").unlink()
    result = run(repo, "foo.txt", "-m", "remove foo")
    assert result.returncode == 0, result.stderr
    assert log_subjects(repo)[0] == "remove foo"
    assert not (repo / "foo.txt").exists()


def test_dash_dash_separator_treats_rest_as_files(repo):
    (repo / "-oddname.txt").write_text("hi\n")
    result = run(repo, "-m", "odd file", "--", "-oddname.txt")
    assert result.returncode == 0, result.stderr
    assert log_subjects(repo) == ["odd file"]


def test_dash_c_targets_another_repo(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    init_repo(other)
    (other / "foo.txt").write_text("hi\n")
    result = subprocess.run(
        [str(SCRIPT), "foo.txt", "-m", "in other repo", "-C", str(other)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert log_subjects(other) == ["in other repo"]


def test_help_exits_zero_and_does_not_touch_repo(repo):
    (repo / "foo.txt").write_text("hi\n")
    run(repo, "foo.txt", "-m", "seed commit")
    result = run(repo, "--help")
    assert result.returncode == 0
    assert "Usage: git-commit" in result.stdout
    assert "--amend" not in result.stdout
    assert log_subjects(repo) == ["seed commit"]


def test_successful_commit_on_main_pushes_to_origin(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    init_repo(work)
    remote = seed_origin(work, tmp_path)
    (work / "foo.txt").write_text("hi\n")

    result = run(work, "foo.txt", "-m", "push me")

    assert result.returncode == 0, result.stderr
    local_sha = subprocess.run(
        [REAL_GIT, "rev-parse", "HEAD"],
        cwd=work,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    remote_sha = subprocess.run(
        [REAL_GIT, "rev-parse", "main"],
        cwd=remote,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert remote_sha == local_sha
    assert f"Commit succeeded and pushed: {local_sha}" in result.stdout


def test_failed_push_retries_and_can_succeed(tmp_path, push_shim):
    work = tmp_path / "work"
    work.mkdir()
    init_repo(work)
    remote = seed_origin(work, tmp_path)
    (work / "foo.txt").write_text("hi\n")

    result = run(work, "foo.txt", "-m", "retry me", env=push_shim("retry"))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "retry.count").read_text() == "3"
    local_sha = subprocess.run(
        [REAL_GIT, "rev-parse", "HEAD"],
        cwd=work,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    remote_sha = subprocess.run(
        [REAL_GIT, "rev-parse", "main"],
        cwd=remote,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert remote_sha == local_sha


def test_apparent_push_failure_verified_present_is_success(tmp_path, push_shim):
    work = tmp_path / "work"
    work.mkdir()
    init_repo(work)
    seed_origin(work, tmp_path)
    (work / "foo.txt").write_text("hi\n")

    result = run(work, "foo.txt", "-m", "land ambiguously", env=push_shim("present"))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "present.count").read_text() == "3"
    assert "verified to already have this commit" in result.stdout


def test_failed_push_verified_absent_is_incomplete(tmp_path, push_shim):
    work = tmp_path / "work"
    work.mkdir()
    init_repo(work)
    seed_origin(work, tmp_path)
    (work / "foo.txt").write_text("hi\n")

    result = run(work, "foo.txt", "-m", "stay local", env=push_shim("absent"))

    assert result.returncode == 1
    assert (tmp_path / "absent.count").read_text() == "3"
    assert "simulated push failure 3" in result.stderr
    assert "simulated push failure 2" not in result.stderr
    assert "Confirmed: origin/main does NOT have" in result.stderr
    assert "local is ahead by 1 commit(s)" in result.stderr


def test_failed_push_and_fetch_reports_unknown(tmp_path, push_shim):
    work = tmp_path / "work"
    work.mkdir()
    init_repo(work)
    seed_origin(work, tmp_path)
    (work / "foo.txt").write_text("hi\n")

    result = run(work, "foo.txt", "-m", "cannot verify", env=push_shim("unknown"))

    assert result.returncode == 1
    assert (tmp_path / "unknown.count").read_text() == "3"
    assert "simulated push failure 3" in result.stderr
    assert "simulated fetch failure" in result.stderr
    assert "Push status is UNKNOWN" in result.stderr


def test_non_main_branch_does_not_push(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    init_repo(work)
    remote = seed_origin(work, tmp_path)
    remote_before = subprocess.run(
        [REAL_GIT, "rev-parse", "main"],
        cwd=remote,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    subprocess.run([REAL_GIT, "checkout", "-q", "-b", "feature"], cwd=work, check=True)
    (work / "foo.txt").write_text("hi\n")

    result = run(work, "foo.txt", "-m", "feature only")

    assert result.returncode == 0, result.stderr
    remote_after = subprocess.run(
        [REAL_GIT, "rev-parse", "main"],
        cwd=remote,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert remote_after == remote_before


def test_main_without_origin_remains_commit_only(repo):
    subprocess.run([REAL_GIT, "checkout", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "foo.txt").write_text("hi\n")

    result = run(repo, "foo.txt", "-m", "local only")

    assert result.returncode == 0, result.stderr
    assert "pushed" not in result.stdout


class TestDishVersionGuard:
    """DISH_VERSION governs dish-task-schema.json and dish-schema-migrations/."""

    def _seed(self, repo, protocol="1", schema="1"):
        (repo / "DISH_VERSION").write_text(f"PROTOCOL_VERSION={protocol}\nSCHEMA_VERSION={schema}\n")
        (repo / "dish-task-schema.json").write_text(
            '{\n  "a": 1,\n  "protocol_version": "%s"\n}\n' % protocol
        )
        result = run(repo, "DISH_VERSION", "dish-task-schema.json", "-m", "baseline")
        assert result.returncode == 0, result.stderr

    def test_unrelated_files_are_unaffected(self, repo):
        (repo / "foo.txt").write_text("hi\n")
        result = run(repo, "foo.txt", "-m", "no schema here")
        assert result.returncode == 0, result.stderr

    def test_initial_baseline_has_no_prior_to_compare(self, repo):
        (repo / "DISH_VERSION").write_text("PROTOCOL_VERSION=1\nSCHEMA_VERSION=1\n")
        (repo / "dish-task-schema.json").write_text('{"a": 1}\n')
        result = run(repo, "DISH_VERSION", "dish-task-schema.json", "-m", "baseline")
        assert result.returncode == 0, result.stderr

    def test_structural_schema_change_without_version_bump_is_rejected(self, repo):
        self._seed(repo)
        (repo / "dish-task-schema.json").write_text('{"a": 2}\n')
        result = run(repo, "dish-task-schema.json", "-m", "change schema, forget bump")
        assert result.returncode == 1
        assert "require both" in result.stderr

    def test_structural_schema_change_with_both_bumps_succeeds(self, repo):
        self._seed(repo)
        (repo / "dish-task-schema.json").write_text('{"a": 2}\n')
        (repo / "DISH_VERSION").write_text("PROTOCOL_VERSION=2\nSCHEMA_VERSION=2\n")
        result = run(
            repo, "dish-task-schema.json", "DISH_VERSION", "-m", "bump both"
        )
        assert result.returncode == 0, result.stderr

    def test_only_protocol_version_bumped_is_rejected(self, repo):
        self._seed(repo)
        (repo / "dish-task-schema.json").write_text('{"a": 2}\n')
        (repo / "DISH_VERSION").write_text("PROTOCOL_VERSION=2\nSCHEMA_VERSION=1\n")
        result = run(
            repo, "dish-task-schema.json", "DISH_VERSION", "-m", "only protocol bumped"
        )
        assert result.returncode == 1
        assert "require both" in result.stderr

    def test_protocol_version_only_change_is_not_structural(self, repo):
        self._seed(repo)
        (repo / "dish-task-schema.json").write_text(
            '{\n  "a": 1,\n  "protocol_version": "2"\n}\n'
        )
        result = run(repo, "dish-task-schema.json", "-m", "protocol_version bump only")
        assert result.returncode == 0, result.stderr

    def test_schema_change_without_staged_dish_version_is_rejected(self, repo):
        self._seed(repo)
        (repo / "dish-task-schema.json").write_text('{"a": 2}\n')
        result = run(repo, "dish-task-schema.json", "-m", "no DISH_VERSION staged at all")
        assert result.returncode == 1
        assert "require both" in result.stderr

    def test_new_file_under_schema_migrations_requires_bump(self, repo):
        self._seed(repo)
        (repo / "dish-schema-migrations").mkdir()
        (repo / "dish-schema-migrations" / "0002.py").write_text("# migration\n")
        result = run(repo, "dish-schema-migrations/0002.py", "-m", "new migration, no bump")
        assert result.returncode == 1
        assert "require both" in result.stderr
