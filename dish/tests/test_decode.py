"""CLI-arg vs. JSON-decoded string paths (asana-cli-fixes.md P0 #2 / P1 #5).

_text() decodes literal backslash-n sequences from CLI args (and stdin) into
real newlines. That decoding must not be re-applied to values that came from
a batch-file JSON payload, since json.load() has already resolved any JSON
string escapes -- re-running _text() on that output can mangle content the
user genuinely wanted to contain a literal backslash-n.
"""
import json


def test_cli_arg_literal_backslash_n_decodes_to_newline(cli):
    assert cli._text("line1\\nline2") == "line1\nline2"


def test_batch_json_actual_newline_passed_through_unchanged(cli):
    """A JSON source with a real \\n escape decodes to an actual newline
    character via json.load(); that must survive batch normalization as-is."""
    raw_json = json.dumps({
        "operations": [{
            "action": "set_notes",
            "task": "123",
            "notes": "line1\nline2",  # real newline, one JSON escape
            "reason": "test",
        }]
    })
    plan = json.loads(raw_json)
    op = cli._normalize_batch_op(plan["operations"][0])
    assert op["fields"]["notes"] == "line1\nline2"


def test_batch_json_literal_backslash_n_round_trips_unchanged(cli):
    """Regression case: the user wants a literal backslash-n inside notes
    (e.g. documenting an escape sequence), encoded in JSON as \\\\n so
    json.load() decodes it to the two characters backslash + n. Re-running
    _text()'s decoding on this (the bug) would corrupt it into a real
    newline. It must round-trip unchanged."""
    raw_json = json.dumps({
        "operations": [{
            "action": "set_notes",
            "task": "123",
            "notes": "path is C:\\\\notes\\\\file",  # JSON: C:\notes\file (literal \n inside "notes")
            "reason": "test",
        }]
    })
    plan = json.loads(raw_json)
    original = plan["operations"][0]["notes"]
    assert "\\n" in original  # sanity: json.load gave us a literal backslash-n substring
    assert "\n" not in original  # and NOT an actual newline

    op = cli._normalize_batch_op(plan["operations"][0])

    assert op["fields"]["notes"] == original


def test_batch_json_literal_backslash_n_in_replace_notes_round_trips(cli):
    raw_json = json.dumps({
        "operations": [{
            "action": "replace_notes",
            "task": "123",
            "old": "old C:\\\\notes\\\\file",
            "new": "new C:\\\\notes\\\\file",
            "reason": "test",
        }]
    })
    plan = json.loads(raw_json)
    orig_old = plan["operations"][0]["old"]
    orig_new = plan["operations"][0]["new"]

    op = cli._normalize_batch_op(plan["operations"][0])

    assert op["old"] == orig_old
    assert op["new"] == orig_new


def test_batch_json_literal_backslash_n_in_create_task_notes_round_trips(cli):
    raw_json = json.dumps({
        "operations": [{
            "action": "create_task",
            "project": "1",
            "name": "t",
            "notes": "C:\\\\notes\\\\file",
            "reason": "test",
        }]
    })
    plan = json.loads(raw_json)
    original = plan["operations"][0]["notes"]

    op = cli._normalize_batch_op(plan["operations"][0])

    assert op["notes"] == original
