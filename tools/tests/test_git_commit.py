"""Black-box tests for tools/git-commit.

Runs the real script as a subprocess against throwaway git repos in tmp_path,
rather than importing it, since its behavior is defined by what it does to a
real git index/working tree -- staging, amending, the DISH_VERSION guard --
not by any internal function contract.
"""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "git-commit"


def run(repo, *args):
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=repo,
        text=True,
        capture_output=True,
    )


def init_repo(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)


def log_subjects(repo):
    result = subprocess.run(
        ["git", "log", "--format=%s"], cwd=repo, check=True, text=True, capture_output=True
    )
    return result.stdout.splitlines()


@pytest.fixture
def repo(tmp_path):
    init_repo(tmp_path)
    return tmp_path


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


def test_requires_message_unless_amending(repo):
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


def test_amend_with_new_message(repo):
    (repo / "foo.txt").write_text("hi\n")
    run(repo, "foo.txt", "-m", "first")
    (repo / "foo.txt").write_text("hi again\n")
    result = run(repo, "foo.txt", "--amend", "-m", "amended")
    assert result.returncode == 0, result.stderr
    assert log_subjects(repo) == ["amended"]


def test_amend_without_message_keeps_prior_message(repo):
    (repo / "foo.txt").write_text("hi\n")
    run(repo, "foo.txt", "-m", "keep me")
    (repo / "foo.txt").write_text("hi again\n")
    result = run(repo, "foo.txt", "--amend")
    assert result.returncode == 0, result.stderr
    assert log_subjects(repo) == ["keep me"]


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
    assert log_subjects(repo) == ["seed commit"]


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
