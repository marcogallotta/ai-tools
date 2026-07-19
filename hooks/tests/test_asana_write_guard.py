"""Tests for asana-write-guard: PreToolUse(Bash) guard that asks/denies for
Asana CLI writes (direct invocation, hidden in scripts, or batch-apply), and
asks for Plant-monitoring API writes.
"""
import io
import json
from types import SimpleNamespace

import pytest


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


class TestDirectWrites:
    def test_set_notes_asked(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "asana set-notes 123 'hi'", monkeypatch, capsys)
        assert_asked(decision, "Approve this Asana write")

    def test_rename_asked(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "asana rename 123 'new name'", monkeypatch, capsys)
        assert_asked(decision, "Approve this Asana write")

    def test_path_qualified_binary_asked(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "/home/marco/.claude/bin/asana move 1 2", monkeypatch, capsys)
        assert_asked(decision, "Approve this Asana write")

    def test_raw_post_asked(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "asana raw POST /tasks/123", monkeypatch, capsys)
        assert_asked(decision, "Approve this Asana write")

    def test_raw_get_allowed(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "asana raw GET /tasks/123", monkeypatch, capsys)
        assert_allowed(decision)

    def test_read_subcommand_allowed(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "asana notes 123", monkeypatch, capsys)
        assert_allowed(decision)

    def test_get_subcommand_allowed(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "asana get 123", monkeypatch, capsys)
        assert_allowed(decision)


class TestQuotedTextFalsePositives:
    def test_descriptive_text_mentioning_rename_allowed(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(
            asana_write_guard, 'echo "remember to asana rename this task later"', monkeypatch, capsys
        )
        assert_allowed(decision)

    def test_grep_pattern_mentioning_asana_move_allowed(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(
            asana_write_guard, 'grep -n "asana move" notes.md', monkeypatch, capsys
        )
        assert_allowed(decision)


class TestArgvListForm:
    def test_python_list_literal_write_denied_when_embedded_in_script(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        script = tmp_path / "helper.py"
        script.write_text('subprocess.run(["/path/to/asana", "rename", "1", "x"])\n')
        decision = run_hook(asana_write_guard, f"python3 {script}", monkeypatch, capsys)
        assert_denied(decision, "hidden Asana writes")

    def test_python_list_literal_raw_write_denied_when_embedded_in_script(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        script = tmp_path / "helper.py"
        script.write_text('subprocess.run(["asana", "raw", "POST", "/tasks/1"])\n')
        decision = run_hook(asana_write_guard, f"python3 {script}", monkeypatch, capsys)
        assert_denied(decision, "hidden Asana writes")


class TestHiddenScriptWrites:
    def test_bash_script_with_asana_write_denied(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        script = tmp_path / "run.sh"
        script.write_text("#!/usr/bin/env bash\nasana rename 123 'x'\n")
        decision = run_hook(asana_write_guard, f"bash {script}", monkeypatch, capsys)
        assert_denied(decision, "hidden Asana writes")

    def test_script_with_only_reads_allowed(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        script = tmp_path / "run.sh"
        script.write_text("#!/usr/bin/env bash\nasana get 123\n")
        decision = run_hook(asana_write_guard, f"bash {script}", monkeypatch, capsys)
        assert_allowed(decision)

    def test_env_runner_script_with_write_denied(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        script = tmp_path / "run.py"
        script.write_text("import subprocess\nsubprocess.run(['asana', 'create-task', 'proj', 'title'])\n")
        decision = run_hook(asana_write_guard, f"env python3 {script}", monkeypatch, capsys)
        assert_denied(decision, "hidden Asana writes")

    def test_direct_path_script_with_write_denied(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        script = tmp_path / "run.sh"
        script.write_text("asana append 123 'x'\n")
        decision = run_hook(asana_write_guard, str(script), monkeypatch, capsys)
        assert_denied(decision, "hidden Asana writes")

    def test_asana_binary_itself_not_treated_as_hidden_script(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        # basename "asana" is excluded from script_from_command so a direct
        # `asana <subcommand>` invocation isn't misread as "run a script
        # named asana" - it's handled by the direct-write check instead.
        fake_asana = tmp_path / "asana"
        fake_asana.write_text("#!/usr/bin/env bash\necho hi\n")
        decision = run_hook(asana_write_guard, f"bash {fake_asana} get 123", monkeypatch, capsys)
        assert_allowed(decision)

    def test_nonexistent_script_path_allowed(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "bash /no/such/script.sh", monkeypatch, capsys)
        assert_allowed(decision)


class TestBatchApply:
    def _plan(self, tmp_path, operations):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"operations": operations}))
        return plan

    def _stub_preview_ok(self, monkeypatch, asana_write_guard):
        monkeypatch.setattr(
            asana_write_guard, "run_batch_preview",
            lambda plan: SimpleNamespace(returncode=0, stdout=""),
        )

    def _stub_preview_fail(self, monkeypatch, asana_write_guard, message):
        monkeypatch.setattr(
            asana_write_guard, "run_batch_preview",
            lambda plan: SimpleNamespace(returncode=1, stdout=f"some earlier output\n{message}\n"),
        )

    def test_valid_batch_asks_with_counts(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        self._stub_preview_ok(monkeypatch, asana_write_guard)
        plan = self._plan(tmp_path, [
            {"task": "1"}, {"task": "2"}, {"task": "3"},
        ])
        decision = run_hook(asana_write_guard, f"asana batch-apply {plan}", monkeypatch, capsys)
        assert_asked(decision, "3 operations across 3 targets")

    def test_batch_dedupes_targets(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        self._stub_preview_ok(monkeypatch, asana_write_guard)
        plan = self._plan(tmp_path, [
            {"task": "1"}, {"task": "1"}, {"parent": "9"},
        ])
        decision = run_hook(asana_write_guard, f"asana batch-apply {plan}", monkeypatch, capsys)
        assert_asked(decision, "3 operations across 2 targets")

    def test_too_few_operations_denied(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        plan = self._plan(tmp_path, [{"task": "1"}, {"task": "2"}])
        decision = run_hook(asana_write_guard, f"asana batch-apply {plan}", monkeypatch, capsys)
        assert_denied(decision, "at least 3 operations")

    def test_preview_failure_denied_with_last_line(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        self._stub_preview_fail(monkeypatch, asana_write_guard, "Error: task 999 not found")
        plan = self._plan(tmp_path, [{"task": "1"}, {"task": "2"}, {"task": "3"}])
        decision = run_hook(asana_write_guard, f"asana batch-apply {plan}", monkeypatch, capsys)
        assert_denied(decision, "Error: task 999 not found")

    def test_extra_args_denied_as_not_direct_invocation(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        plan = self._plan(tmp_path, [{"task": "1"}, {"task": "2"}, {"task": "3"}])
        decision = run_hook(asana_write_guard, f"asana batch-apply {plan} --extra", monkeypatch, capsys)
        assert_denied(decision, "exactly one plan path")

    def test_smuggled_leading_command_denied(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        # Closes the loophole in the old bash version: a compound command
        # with something prepended before "asana batch-apply <path>" must
        # not be accepted as the strict single-invocation form.
        plan = self._plan(tmp_path, [{"task": "1"}, {"task": "2"}, {"task": "3"}])
        decision = run_hook(asana_write_guard, f"cd /tmp; asana batch-apply {plan}", monkeypatch, capsys)
        assert_denied(decision, "exactly one plan path")

    def test_missing_plan_file_denied(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "asana batch-apply /no/such/plan.json", monkeypatch, capsys)
        assert_denied(decision, "at least 3 operations")

    def test_bash_prefix_form_allowed_through(self, asana_write_guard, monkeypatch, capsys, tmp_path):
        self._stub_preview_ok(monkeypatch, asana_write_guard)
        plan = self._plan(tmp_path, [{"task": "1"}, {"task": "2"}, {"task": "3"}])
        decision = run_hook(asana_write_guard, f"bash /home/marco/.claude/bin/asana batch-apply {plan}", monkeypatch, capsys)
        assert_asked(decision, "3 operations across 3 targets")


class TestApiWrapper:
    def test_post_asked(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "api POST /photos", monkeypatch, capsys)
        assert_asked(decision, "API write")

    def test_get_allowed(self, asana_write_guard, monkeypatch, capsys):
        decision = run_hook(asana_write_guard, "api GET /photos", monkeypatch, capsys)
        assert_allowed(decision)


class TestMissingCommand:
    def test_missing_tool_input_command_allowed(self, asana_write_guard, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_input": {}})))
        exit_code = asana_write_guard.main()
        out = capsys.readouterr().out
        assert exit_code == 0
        assert out.strip() == ""
