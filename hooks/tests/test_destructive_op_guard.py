"""Tests for destructive-op-guard: PreToolUse(Bash) guard that asks/denies
for rm, docker volume removal, destructive git subcommands, psql writes,
rsync --delete, and ssh with a remote command.
"""
import io
import json

import pytest


def run_hook(module, command, monkeypatch, capsys, cwd=None):
    payload = {"tool_input": {"command": command}}
    payload["cwd"] = cwd or "/tmp"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    exit_code = module.main()
    out = capsys.readouterr().out
    assert exit_code == 0
    return json.loads(out) if out.strip() else None


def assert_allowed(decision):
    assert decision is None or decision["hookSpecificOutput"]["permissionDecision"] == "allow"


def assert_asked(decision, substring):
    assert decision is not None
    output = decision["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "ask"
    assert substring in output["permissionDecisionReason"]


def assert_denied(decision, substring):
    assert decision is not None
    output = decision["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert substring in output["permissionDecisionReason"]


class TestSplitSegments:
    def test_no_separators_single_segment(self, destructive_op_guard):
        assert destructive_op_guard.split_segments("echo hi") == ["echo hi"]

    def test_splits_on_semicolon_pipe_amp(self, destructive_op_guard):
        segs = destructive_op_guard.split_segments("a ; b | c & d")
        assert [s.strip() for s in segs] == ["a", "b", "c", "d"]

    def test_double_ampersand_yields_empty_segment(self, destructive_op_guard):
        segs = [s for s in destructive_op_guard.split_segments("a && b") if s.strip()]
        assert [s.strip() for s in segs] == ["a", "b"]

    def test_separator_inside_single_quotes_not_split(self, destructive_op_guard):
        segs = destructive_op_guard.split_segments("echo 'a; b | c'")
        assert len(segs) == 1

    def test_separator_inside_double_quotes_not_split(self, destructive_op_guard):
        segs = destructive_op_guard.split_segments('echo "a; b | c"')
        assert len(segs) == 1

    def test_bare_newline_splits_like_semicolon(self, destructive_op_guard):
        segs = destructive_op_guard.split_segments("a\nb")
        assert [s.strip() for s in segs] == ["a", "b"]

    def test_backslash_newline_continuation_stays_one_segment(self, destructive_op_guard):
        segs = destructive_op_guard.split_segments("a --foo \\\n  --bar")
        assert len(segs) == 1
        assert segs[0] == "a --foo   --bar"

    def test_newline_inside_double_quotes_not_split(self, destructive_op_guard):
        segs = destructive_op_guard.split_segments('echo "a\nb"')
        assert len(segs) == 1


class TestGitFalsePositiveRepros:
    # Real repro from a live session: a destructive word appearing only
    # inside a quoted grep pattern, with no actual git invocation anywhere.
    def test_grep_pattern_containing_git_commit_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard,
            'grep -rln "commit wrapper\\|git-commit" --include="*.md" .',
            monkeypatch,
            capsys,
        )
        assert_allowed(decision)

    def test_grep_pattern_with_git_commit_and_wrapper_word_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard,
            'grep -n "git-commit\\|complexity.budget\\|wrap.*count\\|line count\\|wrapper" /home/marco/.claude/CLAUDE.md',
            monkeypatch,
            capsys,
        )
        assert_allowed(decision)

    def test_echo_of_git_commit_words_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, 'echo "git commit"', monkeypatch, capsys)
        assert_allowed(decision)

    # Real repro: check_git's "add" scan matched the token anywhere in the
    # command, not just git's own subcommand position, so any subcommand
    # that happens to take a literal "add" argument (worktree/remote/
    # submodule) false-triggered the "don't run git add alone" denial.
    def test_git_worktree_add_unknown_asks(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git worktree add ../foo agent/some-branch", monkeypatch, capsys
        )
        assert_asked(decision, "Destructive git operation")

    def test_git_remote_add_unknown_asks(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git remote add origin git@example.com:x/y.git", monkeypatch, capsys
        )
        assert_asked(decision, "Destructive git operation")

    def test_git_submodule_add_unknown_asks(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git submodule add https://example.com/x.git", monkeypatch, capsys
        )
        assert_asked(decision, "Destructive git operation")


class TestRm:
    def test_rm_tmp_target_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rm -rf /tmp/foo", monkeypatch, capsys)
        assert_allowed(decision)

    def test_rm_multiple_tmp_targets_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rm -rf /tmp/foo /tmp/bar", monkeypatch, capsys)
        assert_allowed(decision)

    def test_rm_non_tmp_target_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rm -rf foo", monkeypatch, capsys)
        assert_asked(decision, "'rm' requires explicit approval")

    def test_rm_mixed_tmp_and_non_tmp_targets_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rm -rf /tmp/foo bar", monkeypatch, capsys)
        assert_asked(decision, "'rm' requires explicit approval")

    def test_rm_no_resolvable_target_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rm -rf", monkeypatch, capsys)
        assert_asked(decision, "'rm' requires explicit approval")

    def test_npm_not_confused_with_rm(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "npm install", monkeypatch, capsys)
        assert_allowed(decision)

    def test_rm_inside_quoted_string_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, 'echo "rm -rf /"', monkeypatch, capsys)
        assert_allowed(decision)

    def test_docker_rm_container_not_confused_with_rm(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "docker rm pg17-extract", monkeypatch, capsys)
        assert_allowed(decision)

    def test_docker_volume_rm_not_confused_with_rm(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "docker volume rm myvol", monkeypatch, capsys)
        assert_allowed(decision)

    # Real repro: a bare rm of a /tmp/ target stacked on a plain newline
    # above an unrelated multi-line command. Before split_segments handled
    # bare newlines, both lines were tokenized as one segment and the second
    # command's --pg-bin argument (a non-/tmp path) was misread as another
    # rm target, wrongly triggering approval despite the rm target being
    # exempt.
    def test_rm_tmp_target_stacked_above_unrelated_command_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard,
            "rm -rf /tmp/scratchpad/section2-work\n"
            ".venv/bin/python scripts/dish-pg-recovery-rehearsal \\\n"
            "  --report /tmp/scratchpad/section2-report.json \\\n"
            "  --pg-bin /usr/lib/postgresql/17/bin",
            monkeypatch,
            capsys,
        )
        assert_allowed(decision)

    def test_sudo_rm_non_tmp_target_still_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "sudo rm -rf foo", monkeypatch, capsys)
        assert_asked(decision, "'rm' requires explicit approval")

    def test_sudo_rm_tmp_target_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "sudo rm -rf /tmp/foo", monkeypatch, capsys)
        assert_allowed(decision)

    def test_rm_downloads_tilde_target_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rm -rf ~/Downloads/foo.zip", monkeypatch, capsys)
        assert_allowed(decision)

    def test_rm_downloads_absolute_target_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rm -rf /home/marco/Downloads/foo.zip", monkeypatch, capsys)
        assert_allowed(decision)

    def test_rm_mixed_tmp_and_downloads_targets_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rm -rf /tmp/foo ~/Downloads/bar", monkeypatch, capsys)
        assert_allowed(decision)


class TestDocker:
    def test_compose_down_v_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "docker compose down -v", monkeypatch, capsys)
        assert_asked(decision, "docker compose down -v")

    def test_compose_down_without_v_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "docker compose down", monkeypatch, capsys)
        assert_allowed(decision)

    def test_compose_up_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "docker compose up -d", monkeypatch, capsys)
        assert_allowed(decision)


class TestGitCommitWrapper:
    def test_wrapper_invocation_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, 'git-commit foo.py -m "msg"', monkeypatch, capsys)
        assert_asked(decision, "git-commit (stage + commit)")

    def test_wrapper_help_long_flag_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git-commit --help", monkeypatch, capsys)
        assert_allowed(decision)

    def test_wrapper_help_short_flag_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git-commit -h", monkeypatch, capsys)
        assert_allowed(decision)

    def test_wrapper_path_qualified_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "/home/marco/.claude/bin/git-commit foo.py -m x", monkeypatch, capsys
        )
        assert_asked(decision, "git-commit (stage + commit)")

    def test_bare_wrapper_invocation_allowed(self, destructive_op_guard, monkeypatch, capsys):
        # Matches the original bash regex quirk: with no following token
        # there's nothing after "git-commit" to require a space before, so
        # it's never flagged. Preserved rather than "fixed" since it's not
        # in scope for this port.
        decision = run_hook(destructive_op_guard, "git-commit", monkeypatch, capsys)
        assert_allowed(decision)


class TestGitSubcommands:
    def test_git_commit_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, 'git commit -m "x"', monkeypatch, capsys)
        assert_asked(decision, "Destructive git operation")

    def test_git_push_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git push", monkeypatch, capsys)
        assert_asked(decision, "Destructive git operation")

    def test_git_checkout_asked(self, destructive_op_guard, monkeypatch, capsys):
        # cwd is outside any git repo, so the new protected-checkout branch
        # isolation check (which needs to resolve a real repo identity) finds
        # nothing to act on and this exercises check_git's own generic ask.
        decision = run_hook(destructive_op_guard, "git checkout main", monkeypatch, capsys, cwd="/tmp")
        assert_asked(decision, "Destructive git operation")

    def test_git_clean_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git clean -fd", monkeypatch, capsys)
        assert_asked(decision, "Destructive git operation")

    def test_git_restore_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git restore file.py", monkeypatch, capsys)
        assert_asked(decision, "Destructive git operation")

    def test_git_status_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git status", monkeypatch, capsys)
        assert_allowed(decision)

    def test_dash_c_read_compound_explicitly_allowed(
        self, destructive_op_guard, monkeypatch, capsys
    ):
        command = "git -C /tmp status --short && git -C /tmp branch --show-current"
        decision = run_hook(destructive_op_guard, command, monkeypatch, capsys)
        assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_git_reset_hard_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git reset --hard HEAD~1", monkeypatch, capsys)
        assert_asked(decision, "Destructive git operation")

    def test_git_reset_without_repo_asks(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git reset HEAD~1", monkeypatch, capsys)
        assert_asked(decision, "Destructive git operation")

    def test_git_add_denied(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git add foo.py", monkeypatch, capsys)
        assert_denied(decision, "Don't run git add alone")

    def test_git_dash_c_repo_add_still_denied(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git -C /some/repo add foo.py", monkeypatch, capsys)
        assert_denied(decision, "Don't run git add alone")

    def test_git_dash_c_repo_worktree_add_asks(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git -C /some/repo worktree add ../foo agent/x", monkeypatch, capsys
        )
        assert_asked(decision, "Destructive git operation")


class TestPsql:
    def test_write_keyword_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, 'psql -d mydb -c "DROP TABLE x"', monkeypatch, capsys)
        assert_asked(decision, "psql write operation")

    def test_inline_sql_flag_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, 'psql -c "SELECT 1"', monkeypatch, capsys)
        assert_asked(decision, "psql -c")

    def test_psql_from_file_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "psql -d mydb -f schema.sql", monkeypatch, capsys)
        assert_allowed(decision)


class TestRsync:
    def test_delete_flag_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rsync -av --delete src/ dst/", monkeypatch, capsys)
        assert_asked(decision, "rsync --delete")

    def test_without_delete_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "rsync -av src/ dst/", monkeypatch, capsys)
        assert_allowed(decision)


class TestSsh:
    def test_interactive_login_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "ssh myhost", monkeypatch, capsys)
        assert_allowed(decision)

    def test_remote_command_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, 'ssh myhost "ls -la"', monkeypatch, capsys)
        assert_asked(decision, "ssh with remote command")

    def test_trusted_host_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, 'ssh plantpi.local "reboot"', monkeypatch, capsys)
        assert_allowed(decision)

    def test_value_flag_then_host_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "ssh -p 22 myhost", monkeypatch, capsys)
        assert_allowed(decision)

    def test_value_flag_then_host_and_command_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "ssh -p 22 myhost uptime", monkeypatch, capsys)
        assert_asked(decision, "ssh with remote command")


class TestSegmentsAndCompoundCommands:
    def test_only_second_segment_destructive_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, 'git status; git commit -m "x"', monkeypatch, capsys)
        assert_asked(decision, "Destructive git operation")

    def test_all_segments_benign_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "ls; rm -rf /tmp/foo", monkeypatch, capsys)
        assert_allowed(decision)

    def test_second_segment_rm_non_tmp_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "ls; rm -rf foo", monkeypatch, capsys)
        assert_asked(decision, "'rm' requires explicit approval")


class TestMissingCommand:
    def test_missing_tool_input_command_allowed(self, destructive_op_guard, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_input": {}})))
        exit_code = destructive_op_guard.main()
        out = capsys.readouterr().out
        assert exit_code == 0
        assert out.strip() == ""


class TestProtectedCheckoutBranchIsolation:
    def test_checkout_dash_b_in_primary_denied(self, destructive_op_guard, protected_repo, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_switch_dash_c_in_primary_denied(self, destructive_op_guard, protected_repo, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git switch -c agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'switch' branch change")

    def test_bare_checkout_branch_in_primary_denied(self, destructive_op_guard, protected_repo, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git checkout main", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_switch_plain_branch_in_primary_denied(self, destructive_op_guard, protected_repo, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git switch main", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'switch' branch change")

    def test_dash_c_primary_from_elsewhere_denied(self, destructive_op_guard, protected_repo, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard,
            f"git -C {protected_repo['primary']} checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["unrelated"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_relative_git_dir_before_dash_c_resolves_relative_to_dash_c_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920289206: real git interprets a relative
        # --git-dir/--work-tree value relative to the directory -C lands on,
        # regardless of which option appears first on the command line. A
        # hook that resolves --git-dir against the *original* cwd instead
        # (independent of -C) would miss this and let it fall through.
        decision = run_hook(
            destructive_op_guard,
            "git --git-dir=.git -C primary checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["primary"].parent),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_relative_git_dir_before_dash_c_resolves_to_unrelated_falls_through(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard,
            "git --git-dir=.git -C unrelated checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["unrelated"].parent),
        )
        assert_asked(decision, "Destructive git operation")

    def test_chained_relative_dash_c_resolves_to_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920289206: successive relative -C values
        # chain from the *preceding* -C, not from the original cwd. Here the
        # first -C lands on an unrelated plain directory and the second -C
        # is relative to that, not to the hook's own starting cwd.
        sibling = protected_repo["primary"].parent / "sibling"
        sibling.mkdir()
        decision = run_hook(
            destructive_op_guard,
            "git -C sibling -C ../primary checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["primary"].parent),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_no_approval_path_offered_on_denial(self, destructive_op_guard, protected_repo, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_checkout_in_linked_worktree_is_prompt_free(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "git checkout -b agent/bar", monkeypatch, capsys,
            cwd=str(protected_repo["linked"]),
        )
        assert_allowed(decision)

    def test_checkout_in_unrelated_repo_unaffected(self, destructive_op_guard, protected_repo, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git checkout -b agent/baz", monkeypatch, capsys,
            cwd=str(protected_repo["unrelated"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_file_only_checkout_with_double_dash_not_hard_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "git checkout -- README.md", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_checkout_ref_with_trailing_bare_double_dash_in_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920409151: "git checkout <ref> --" with
        # nothing after "--" still switches branches (verified against real
        # git) - a bare trailing "--" must not be read as "file checkout".
        decision = run_hook(
            destructive_op_guard, "git checkout main --", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_checkout_dash_b_with_trailing_bare_double_dash_in_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "git checkout -b agent/foo --", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_checkout_ref_with_double_dash_and_pathspec_still_file_only(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # A pathspec actually present after "--" is the real file-restore
        # form (verified: does not change the branch) and must stay ASK.
        decision = run_hook(
            destructive_op_guard, "git checkout main -- README.md", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_switch_with_double_dash_separator_in_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920409151: "git switch -- <branch>" still
        # switches (switch has no pathspec-restore mode at all).
        decision = run_hook(
            destructive_op_guard, "git switch -- main", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'switch' branch change")

    def test_switch_dash_c_with_trailing_double_dash_in_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "git switch -c agent/bar --", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'switch' branch change")

    def test_glob_star_in_dash_c_path_denied_ambiguous(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920409151: bash expands "primary*" against
        # the filesystem before git ever sees it, but our own subprocess
        # git rev-parse call (argv list, no shell) receives it as a literal,
        # non-existent path and would otherwise fail to resolve, letting
        # this fall through to ordinary ASK.
        decision = run_hook(
            destructive_op_guard, "git -C primary* checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"].parent),
        )
        assert_denied(decision, "unresolvable repository-location override")

    def test_glob_question_mark_in_git_dir_denied_ambiguous(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "git --git-dir=primar?/.git checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"].parent),
        )
        assert_denied(decision, "unresolvable repository-location override")

    def test_glob_bracket_in_env_override_not_ambiguous_falls_through(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Fix for review 4920522911: bash does NOT pathname-expand a
        # VAR=value assignment's right-hand side (verified against real
        # bash), so "[y]" here is always literal regardless of quoting -
        # this resolves as the literal (nonexistent) path "primar[y]/.git"
        # and correctly falls through to ordinary ask, not ambiguous-deny.
        decision = run_hook(
            destructive_op_guard, "GIT_DIR=primar[y]/.git git checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"].parent),
        )
        assert_asked(decision, "Destructive git operation")

    def test_quoted_bracket_glob_in_dash_c_not_ambiguous_falls_through(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Fix for review 4920522911: a single-quoted glob character is
        # never expansion-eligible - shlex-level quote removal makes
        # 'repo[1]' and repo[1] look identical as plain text, so the fix
        # must track quoting, not just scan the final token text.
        decision = run_hook(
            destructive_op_guard, "git -C 'repo[1]' checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_escaped_glob_star_in_dash_c_not_ambiguous_falls_through(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, r"git -C repo\* checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_unquoted_bracket_glob_in_dash_c_still_ambiguous_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Contrast with the quoted/escaped cases above: genuinely unquoted
        # "[" is still pathname-expansion-eligible on the command line and
        # must still fail closed.
        decision = run_hook(
            destructive_op_guard, "git -C repo[1] checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "unresolvable repository-location override")

    def test_quoted_dollar_in_git_dir_env_override_not_ambiguous_falls_through(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "GIT_DIR='$FAKE' git checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_unquoted_dollar_in_git_dir_env_override_still_ambiguous_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "GIT_DIR=$FAKE git checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "unresolvable repository-location override")

    def test_switch_double_dash_then_dash_h_branch_name_in_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920522911: "--" ends option parsing for
        # switch entirely, so "-h" after it is a literal branch name, not a
        # help flag (verified against real git: a ref named "-h", created
        # via update-ref since `git branch` itself refuses the name, really
        # gets switched to by "git switch -- -h").
        decision = run_hook(
            destructive_op_guard, "git switch -- -h", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'switch' branch change")

    def test_switch_double_dash_then_dash_dash_help_branch_name_in_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "git switch -- --help", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'switch' branch change")

    def test_switch_real_help_flag_without_double_dash_still_allowed(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Control: a genuine -h (no preceding "--") is still read as help.
        decision = run_hook(
            destructive_op_guard, "git switch -h", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_allowed(decision)

    def test_read_only_status_in_primary_allowed(self, destructive_op_guard, protected_repo, monkeypatch, capsys):
        decision = run_hook(
            destructive_op_guard, "git status", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_allowed(decision)

    def test_git_dir_env_override_resolving_to_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920042246: GIT_DIR can retarget Git at the
        # protected checkout from a cwd outside it entirely. Resolved via a
        # real git rev-parse against the override, not guessed from cwd.
        decision = run_hook(
            destructive_op_guard,
            f"GIT_DIR={protected_repo['primary']}/.git git checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["unrelated"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_git_dir_env_override_resolving_to_unrelated_repo_falls_through(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Fix for review 4920205286's blocker: a GIT_DIR override that
        # actually, provably resolves to an unrelated repository must retain
        # ordinary behavior (ask), not be blanket-denied.
        decision = run_hook(
            destructive_op_guard,
            f"GIT_DIR={protected_repo['unrelated']}/.git git checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_unresolvable_dash_capital_c_in_primary_denied_ambiguous(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "git -C $SOME_VAR checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "unresolvable repository-location override")

    def test_unresolvable_git_dir_env_override_denied_regardless_of_cwd(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # A GIT_DIR value using shell expansion can't be resolved statically
        # and can't be ruled out as targeting the protected checkout from
        # any cwd, so this still denies unconditionally.
        decision = run_hook(
            destructive_op_guard, "GIT_DIR=$SOME_VAR git checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["unrelated"]),
        )
        assert_denied(decision, "unresolvable repository-location override")

    def test_missing_cwd_with_unresolvable_override_fails_closed_to_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # No cwd in the hook payload at all, combined with an unresolvable
        # override: still denies rather than asks.
        decision = run_hook(
            destructive_op_guard, "GIT_DIR=$SOME_VAR git checkout -b agent/foo", monkeypatch, capsys,
        )
        assert_denied(decision, "unresolvable repository-location override")

    def test_work_tree_env_override_resolving_to_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard,
            f"GIT_WORK_TREE={protected_repo['primary']} GIT_DIR={protected_repo['primary']}/.git "
            "git checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["unrelated"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_dash_c_config_global_option_in_primary_still_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920042246: the separate-token value of
        # "-c" was previously misread as the subcommand, so the actual
        # "checkout -b" further along was never classified at all. -c is
        # config-only, so this now denies via real primary-identity
        # resolution, not the ambiguity path.
        decision = run_hook(
            destructive_op_guard, "git -c color.ui=false checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_dash_c_config_global_option_in_unrelated_repo_not_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920205286's blocker: a benign -c override
        # in an unrelated repository must retain ordinary behavior (ask),
        # not be blanket-denied merely because -c was present.
        decision = run_hook(
            destructive_op_guard, "git -c color.ui=false checkout feature", monkeypatch, capsys,
            cwd=str(protected_repo["unrelated"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_config_env_global_option_in_primary_still_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard,
            "git --config-env core.editor=SOME_ENV checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_config_env_global_option_in_unrelated_repo_not_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard,
            "git --config-env core.editor=SOME_ENV checkout feature",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["unrelated"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_separate_token_git_dir_and_work_tree_resolving_to_primary_denied(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Real repro from review 4920042246: separate-argument --git-dir/
        # --work-tree previously consumed only the flag token, leaving the
        # path value to be misread as the subcommand.
        decision = run_hook(
            destructive_op_guard,
            f"git --git-dir {protected_repo['primary']}/.git --work-tree {protected_repo['primary']} "
            "checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["unrelated"]),
        )
        assert_denied(decision, "Refusing 'checkout' branch change")

    def test_separate_token_git_dir_and_work_tree_resolving_to_unrelated_falls_through(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard,
            f"git --git-dir {protected_repo['unrelated']}/.git --work-tree {protected_repo['unrelated']} "
            "checkout -b agent/foo",
            monkeypatch,
            capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_asked(decision, "Destructive git operation")

    def test_unresolvable_git_dir_flag_denied_ambiguous(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        decision = run_hook(
            destructive_op_guard, "git --git-dir $SOME_VAR checkout -b agent/foo", monkeypatch, capsys,
            cwd=str(protected_repo["unrelated"]),
        )
        assert_denied(decision, "unresolvable repository-location override")

    def test_dash_c_config_option_is_outside_bare_bones_policy(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        # Correct consumption of "-c <value>" must not misclassify an
        # unrelated read-only subcommand as a branch change either.
        decision = run_hook(
            destructive_op_guard, "git -c color.ui=false status", monkeypatch, capsys,
            cwd=str(protected_repo["primary"]),
        )
        assert_asked(decision, "Destructive git operation")


def _register_active_task(protected_repo, monkeypatch, tmp_path, task_gid="12345"):
    import json
    import subprocess

    home = tmp_path / "home"
    root = home / ".local/state/dish/worktrees"
    state_dir = root / task_gid
    state_dir.mkdir(parents=True)
    linked = protected_repo["linked"].resolve()
    git_dir = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--path-format=absolute", "--git-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    common_dir = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (state_dir / ("a" * 24 + "-" + "b" * 32 + ".json")).write_text(json.dumps({
        "task_gid": task_gid,
        "branch": "agent/existing",
        "worktree_path": str(linked),
        "git_dir": git_dir,
        "git_common_dir": common_dir,
        "lifecycle": "active",
    }) + "\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return task_gid


class TestActiveTaskGitBoundary:
    def test_raw_add_commit_push_are_denied_with_canonical_replacement(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys, tmp_path
    ):
        task_gid = _register_active_task(protected_repo, monkeypatch, tmp_path)
        for command, expected in (
            ("git add README.md", "agent-worktree commit"),
            ('git commit -m "x"', "agent-worktree commit"),
            ("git push", "agent-worktree publish"),
        ):
            decision = run_hook(
                destructive_op_guard, command, monkeypatch, capsys,
                cwd=str(protected_repo["linked"]),
            )
            assert_denied(decision, expected)
            assert task_gid in decision["hookSpecificOutput"]["permissionDecisionReason"]

    def test_dash_c_active_task_is_denied_from_elsewhere(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys, tmp_path
    ):
        _register_active_task(protected_repo, monkeypatch, tmp_path)
        decision = run_hook(
            destructive_op_guard,
            f"git -C {protected_repo['linked']} push",
            monkeypatch, capsys, cwd=str(protected_repo["unrelated"]),
        )
        assert_denied(decision, "agent-worktree publish")

    def test_branch_local_reset_is_prompt_free_in_active_task(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys, tmp_path
    ):
        _register_active_task(protected_repo, monkeypatch, tmp_path)
        decision = run_hook(
            destructive_op_guard, "git reset --hard HEAD",
            monkeypatch, capsys, cwd=str(protected_repo["linked"]),
        )
        assert_allowed(decision)

    def test_main_and_explicit_main_still_ask(
        self, destructive_op_guard, protected_repo, monkeypatch, capsys
    ):
        main = run_hook(destructive_op_guard, "git reset --hard HEAD", monkeypatch, capsys,
                        cwd=str(protected_repo["unrelated"]))
        explicit = run_hook(destructive_op_guard, "git branch -D main", monkeypatch, capsys,
                            cwd=str(protected_repo["linked"]))
        assert_asked(main, "Destructive git operation")
        assert_asked(explicit, "Destructive git operation")
