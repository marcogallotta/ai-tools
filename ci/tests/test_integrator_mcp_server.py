from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from integrator_mcp_server import IntegratorReadTools, serve
from integrator_model_contract import INTEGRATOR_PROPOSAL_SCHEMA
from integrator_runtime_config import MCP_TOOLS, prepare_codex_home
from codex_app_server_daemon import CodexDaemonAppServer
from ci_failure_fingerprint import causal_fingerprint
from pr_lifecycle_integrator import IntegratorAudit, consume_projection
from pr_lifecycle_v4 import V4StateStore, actionable_version


FINGERPRINT, _IDENTITY = causal_fingerprint(
    owner_surface="python-control-plane",
    failure_surface="pytest",
    invariant="ci/tests/test_integrator_mcp_server.py::fixture",
    signature="fixture_failure",
)


def case():
    return {
        "case_key": "case-41",
        "reason_class": "CI_OWNERSHIP_AMBIGUOUS",
        "repository": "marcogallotta/ai-tools",
        "pr": 41,
        "head": "a" * 40,
        "evidence": {
            "failure_ownership": "AMBIGUOUS",
            "canonical_ci": {
                "classification": "AMBIGUOUS",
                "candidate_disposition": "BLOCKING",
                "causal_fingerprint": FINGERPRINT,
                "repair_owner_active": False,
                "ownership_evidence": "exact evidence",
                "raw_gate_outcome": "FAILED",
            },
        },
        "next_owner": "Integrator",
        "next_action": "light bounded CI diagnosis",
    }


def projection():
    return {
        "pull_requests": [{
            "number": 41,
            "gate": {
                "diagnosis": "FAILED_REQUIRED_CI",
                "failure_ownership": "AMBIGUOUS",
                "failure_ownership_evidence": "exact evidence",
                "failure_causal_fingerprint": FINGERPRINT,
                "candidate_disposition": "BLOCKING",
            },
        }],
        "v3": {"integrator": {"active_cases": [case()]}},
    }


def configured_tools(tmp_path):
    state = V4StateStore(tmp_path / "state.json")
    state.prepare_wakes([case()])
    audit = IntegratorAudit(tmp_path / "integrator-audit.ndjson", report_path=tmp_path / "integrator-report.json")
    audit.publish_report(consume_projection(projection()).report)
    return IntegratorReadTools(state_dir=tmp_path, repository="marcogallotta/ai-tools")


def test_case_resolution_is_bound_to_existing_v4_receipt(tmp_path):
    tools = configured_tools(tmp_path)
    version = actionable_version(case())
    resolved = tools.get_integrator_case({"actionable_version": version})
    assert resolved["actionable_version"] == version
    assert resolved["receipt_status"] == "PREPARED"
    assert resolved["case"]["head"] == "a" * 40
    assert resolved["canonical_consumer_decision"]["canonical_ci"]["classification"] == "AMBIGUOUS"


def test_exact_pr_tool_refuses_head_movement_before_other_reads(tmp_path, monkeypatch):
    tools = configured_tools(tmp_path)
    calls = []

    def fake(argv):
        calls.append(argv)
        return {"state": "open", "head": {"sha": "b" * 40}}

    monkeypatch.setattr(tools, "_command_json", fake)
    result = tools.get_exact_pr_evidence({"actionable_version": actionable_version(case())})
    assert result["authority_status"] == "stale_head"
    assert len(calls) == 1


def test_check_log_refuses_a_check_from_another_head(tmp_path, monkeypatch):
    tools = configured_tools(tmp_path)
    monkeypatch.setattr(tools, "_command_json", lambda argv: {"head_sha": "b" * 40})
    monkeypatch.setattr(tools, "_command_text", lambda argv: (_ for _ in ()).throw(AssertionError("must not read log")))
    try:
        tools.get_exact_check_log({"actionable_version": actionable_version(case()), "check_run_id": 99})
    except ValueError as exc:
        assert "does not belong" in str(exc)
    else:
        raise AssertionError("wrong-head check log was not refused")


def test_model_tool_audit_binds_exact_input_output_digest_and_turn(tmp_path):
    tools = configured_tools(tmp_path)
    version = actionable_version(case())
    tools.call("get_integrator_case", {"actionable_version": version})
    record = tools.audit.records()[-1]
    assert record["event"] == "model_tool_call"
    assert record["tool_input"] == {"actionable_version": version}
    assert record["tool_output"]["actionable_version"] == version
    assert len(record["tool_output_sha256"]) == 64
    assert record["wake_id"]


def test_isolated_codex_home_has_only_read_tools_and_no_shell(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/integrator_mcp_server.py").write_text("# fixture\n")
    python = tmp_path / "python"
    python.write_text("fixture\n")
    source = tmp_path / "operator-home"
    source.mkdir()
    (source / "auth.json").write_text("{}\n")
    (source / "packages").mkdir()
    home = Path(tempfile.mkdtemp(prefix="di-config-", dir="/tmp"))
    try:
        socket_path = prepare_codex_home(
            codex_home=home,
            source_codex_home=source,
            repo=repo,
            state_dir=tmp_path / "state",
            python=python,
        )
        config = tomllib.loads((home / "config.toml").read_text())
        assert socket_path == home / "app-server-control/app-server-control.sock"
        assert (home / "auth.json").is_symlink()
        assert (home / "packages").is_symlink()
        assert config["web_search"] == "disabled"
        assert config["features"]["shell_tool"] is False
        assert config["features"]["unified_exec"] is False
        assert config["features"]["apps"] is False
        assert config["features"]["plugins"] is False
        assert config["features"]["hooks"] is False
        assert config["agents"]["enabled"] is False
        assert config["memories"]["generate_memories"] is False
        assert config["memories"]["use_memories"] is False
        assert tuple(config["mcp_servers"]["dish_integrator"]["enabled_tools"]) == MCP_TOOLS
    finally:
        shutil.rmtree(home)


def test_mcp_protocol_lists_only_purpose_built_tools(tmp_path, monkeypatch):
    tools = configured_tools(tmp_path)
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        "",
    ])
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(requests))
    monkeypatch.setattr(sys, "stdout", output)
    assert serve(tools) == 0
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item["name"] for item in replies[1]["result"]["tools"]] == list(MCP_TOOLS)
    assert all(item["annotations"]["readOnlyHint"] for item in replies[1]["result"]["tools"])


def test_automated_turn_is_read_only_and_schema_bound():
    client = object.__new__(CodexDaemonAppServer)
    captured = {}

    def request(method, params):
        captured.update(method=method, params=params)
        return {"turn": {"id": "turn-1"}}

    client._request = request
    client.turn_start("thread-1", {"actionable_versions": ["a" * 64]}, client_user_message_id="wake-1")
    assert captured["method"] == "turn/start"
    assert captured["params"]["approvalPolicy"] == "never"
    assert captured["params"]["sandboxPolicy"] == {"type": "readOnly"}
    assert captured["params"]["outputSchema"] == INTEGRATOR_PROPOSAL_SCHEMA
    versions = captured["params"]["outputSchema"]["properties"]["actionable_versions"]
    assert versions == {
        "type": "array",
        "items": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "minItems": 1,
    }
    assert "uniqueItems" not in versions

    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert keys(captured["params"]["outputSchema"]).isdisjoint(
        {"uniqueItems", "minLength", "maxLength", "allOf", "not", "if", "then", "else"}
    )


def test_app_server_completion_wait_uses_notification_without_status_polling():
    client = object.__new__(CodexDaemonAppServer)
    class Socket:
        def settimeout(self, value):
            self.timeout = value
    client.socket = Socket()
    messages = iter([
        json.dumps({"method": "item/completed", "params": {}}),
        json.dumps({
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": "completed"}},
        }),
    ])
    client._read_text = lambda: next(messages)
    assert client.wait_for_turn_completed("turn-1")["status"] == "completed"
