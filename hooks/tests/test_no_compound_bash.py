"""Tests for no-compound-bash: PreToolUse(Bash) guard blocking && / || chaining
and leading VAR=value env prefixes on the first line of a command.
"""
import io
import json

import pytest


def run_hook(module, command, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "tool_input": {"command": command}
    })))
    exit_code = module.main()
    out = capsys.readouterr().out
    assert exit_code == 0
    return json.loads(out) if out.strip() else None


def assert_blocked(decision):
    assert decision is not None
    output = decision["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert "compound/chained bash" in output["permissionDecisionReason"]


def assert_allowed(decision):
    assert decision is None


class TestOutsideQuotes:
    def test_no_quotes_unchanged(self, no_compound_bash):
        assert no_compound_bash.outside_quotes("a && b") == "a && b"

    def test_single_quoted_span_blanked(self, no_compound_bash):
        result = no_compound_bash.outside_quotes("grep 'a && b' file")
        assert "&&" not in result

    def test_double_quoted_span_blanked(self, no_compound_bash):
        result = no_compound_bash.outside_quotes('grep "a && b" file')
        assert "&&" not in result

    def test_single_quote_inside_double_quote_not_toggled(self, no_compound_bash):
        # The " it's " apostrophe shouldn't be treated as a quote delimiter
        # while inside a double-quoted span.
        result = no_compound_bash.outside_quotes('echo "it'"'"'s && ok"')
        assert "&&" not in result

    def test_preserves_length(self, no_compound_bash):
        s = "echo 'x' && echo \"y\""
        assert len(no_compound_bash.outside_quotes(s)) == len(s)


class TestChaining:
    def test_and_and_blocked(self, no_compound_bash, monkeypatch, capsys):
        decision = run_hook(no_compound_bash, "echo a && echo b", monkeypatch, capsys)
        assert_blocked(decision)

    def test_or_or_blocked(self, no_compound_bash, monkeypatch, capsys):
        decision = run_hook(no_compound_bash, "echo a || echo b", monkeypatch, capsys)
        assert_blocked(decision)

    def test_simple_command_allowed(self, no_compound_bash, monkeypatch, capsys):
        decision = run_hook(no_compound_bash, "git status", monkeypatch, capsys)
        assert_allowed(decision)

    def test_and_and_inside_single_quotes_allowed(self, no_compound_bash, monkeypatch, capsys):
        decision = run_hook(
            no_compound_bash, "grep -E 'a && b' file.txt", monkeypatch, capsys
        )
        assert_allowed(decision)

    def test_and_and_inside_double_quotes_allowed(self, no_compound_bash, monkeypatch, capsys):
        decision = run_hook(
            no_compound_bash, 'echo "a && b"', monkeypatch, capsys
        )
        assert_allowed(decision)

    def test_pipe_alone_allowed(self, no_compound_bash, monkeypatch, capsys):
        # Single-command pipes aren't chaining and shouldn't be blocked.
        decision = run_hook(no_compound_bash, "git status | cat", monkeypatch, capsys)
        assert_allowed(decision)

    def test_only_first_line_checked(self, no_compound_bash, monkeypatch, capsys):
        # && on a later line (e.g. inside a heredoc body) must not trigger.
        decision = run_hook(
            no_compound_bash,
            "python - <<'PY'\nprint('a' && 'b')\nPY",
            monkeypatch,
            capsys,
        )
        assert_allowed(decision)

    def test_and_and_on_first_line_of_multiline_blocked(self, no_compound_bash, monkeypatch, capsys):
        decision = run_hook(
            no_compound_bash, "echo a && echo b\necho c", monkeypatch, capsys
        )
        assert_blocked(decision)


class TestEnvPrefix:
    def test_leading_var_assignment_blocked(self, no_compound_bash, monkeypatch, capsys):
        decision = run_hook(no_compound_bash, "FOO=bar echo hi", monkeypatch, capsys)
        assert_blocked(decision)

    def test_leading_var_assignment_no_space_before_value(self, no_compound_bash, monkeypatch, capsys):
        decision = run_hook(no_compound_bash, "FOO=bar", monkeypatch, capsys)
        assert_blocked(decision)

    def test_var_assignment_mid_command_allowed(self, no_compound_bash, monkeypatch, capsys):
        # Only a *leading* VAR=value prefix is blocked, not one appearing later.
        decision = run_hook(no_compound_bash, "echo FOO=bar", monkeypatch, capsys)
        assert_allowed(decision)

    def test_leading_whitespace_before_assignment_blocked(self, no_compound_bash, monkeypatch, capsys):
        decision = run_hook(no_compound_bash, "  FOO=bar echo hi", monkeypatch, capsys)
        assert_blocked(decision)

    def test_command_starting_with_digits_allowed(self, no_compound_bash, monkeypatch, capsys):
        # Not a valid shell identifier, so not treated as a var assignment.
        decision = run_hook(no_compound_bash, "123abc=bar", monkeypatch, capsys)
        assert_allowed(decision)


class TestMissingCommand:
    def test_missing_tool_input_command_allowed(self, no_compound_bash, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_input": {}})))
        exit_code = no_compound_bash.main()
        out = capsys.readouterr().out
        assert exit_code == 0
        assert out.strip() == ""
