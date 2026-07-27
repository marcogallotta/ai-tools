"""Tests for error-message mapping, status filtering, and text/stdin parsing."""
import io

import pytest


def make_error(cli, status=None, reason=None, body=None):
    return cli.ApiException(status=status, reason=reason, body=body)


class TestErrorDetail:
    def test_401(self, cli):
        e = make_error(cli, status=401, body="bad token")
        assert "Asana auth error (401)" in cli._error_detail(e)
        assert "bad token" in cli._error_detail(e)

    def test_404(self, cli):
        e = make_error(cli, status=404, body="not found")
        assert "Asana resource not found (404)" in cli._error_detail(e)

    def test_429(self, cli):
        e = make_error(cli, status=429, body="slow down")
        assert "Asana rate limit (429)" in cli._error_detail(e)

    def test_5xx(self, cli):
        e = make_error(cli, status=503, body="oops")
        assert "Asana server error (503)" in cli._error_detail(e)

    def test_other_status(self, cli):
        e = make_error(cli, status=418, body="teapot")
        assert "Asana API error (418)" in cli._error_detail(e)

    def test_context_included(self, cli):
        e = make_error(cli, status=404, body="not found")
        detail = cli._error_detail(e, context="task 123")
        assert "[task 123]" in detail

    def test_bytes_body_decoded(self, cli):
        e = make_error(cli, status=404, body=b"not found")
        assert "not found" in cli._error_detail(e)

    def test_falls_back_to_reason_when_no_body(self, cli):
        e = make_error(cli, status=500, reason="Internal Server Error", body=None)
        assert "Internal Server Error" in cli._error_detail(e)

    def test_long_body_truncated(self, cli):
        e = make_error(cli, status=500, body="x" * 2000)
        assert len(cli._error_detail(e)) < 1000


class TestStatusMatch:
    def test_both_always_matches(self, cli):
        assert cli._status_match({"completed": True}, "both")
        assert cli._status_match({"completed": False}, "both")

    def test_complete_requires_completed_true(self, cli):
        assert cli._status_match({"completed": True}, "complete")
        assert not cli._status_match({"completed": False}, "complete")

    def test_incomplete_requires_completed_false(self, cli):
        assert cli._status_match({"completed": False}, "incomplete")
        assert not cli._status_match({"completed": True}, "incomplete")

    def test_validate_status_accepts_known_values(self, cli):
        for v in ("incomplete", "complete", "both"):
            assert cli._validate_status(v) == v

    def test_validate_status_rejects_unknown(self, cli):
        with pytest.raises(SystemExit, match="invalid --status"):
            cli._validate_status("done")


class TestTextArg:
    def test_literal_arg_decodes_escaped_newlines(self, cli):
        assert cli._text("line1\\nline2") == "line1\nline2"

    def test_dash_reads_stdin(self, cli, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
        assert cli._text("-") == "from stdin"

    def test_none_reads_stdin(self, cli, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
        assert cli._text(None) == "from stdin"

    def test_plain_text_passthrough(self, cli):
        assert cli._text("plain text") == "plain text"


class TestOpLabel:
    def test_task_target(self, cli):
        assert cli._op_label({"action": "move", "task": "1"}) == "move 1"

    def test_project_target(self, cli):
        assert cli._op_label({"action": "create_task", "project": "9"}) == "create_task 9"

    def test_parent_target(self, cli):
        assert cli._op_label({"action": "create_subtask", "parent": "5"}) == "create_subtask 5"
