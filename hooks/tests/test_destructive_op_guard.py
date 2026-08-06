"""Tests for destructive-op-guard: PreToolUse(Bash) guard that asks/denies
for rm, docker volume removal, destructive git subcommands, psql writes,
rsync --delete, and ssh with a remote command.
"""
import io
import json


def run_hook(module, command, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "tool_input": {"command": command}
    })))
    exit_code = module.main()
    out = capsys.readouterr().out
    assert exit_code == 0
    return json.loads(out) if out.strip() else None


def assert_allowed(decision):
    assert decision is None


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
        decision = run_hook(destructive_op_guard, "git checkout main", monkeypatch, capsys)
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

    def test_git_reset_hard_asked(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git reset --hard HEAD~1", monkeypatch, capsys)
        assert_asked(decision, "reset --hard")

    def test_git_reset_without_hard_allowed(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git reset HEAD~1", monkeypatch, capsys)
        assert_allowed(decision)

    def test_git_add_denied(self, destructive_op_guard, monkeypatch, capsys):
        decision = run_hook(destructive_op_guard, "git add foo.py", monkeypatch, capsys)
        assert_denied(decision, "Don't run git add alone")


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
