from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from dish_tool.cli import build_parser
from dish_tool.admin_cli import build_parser as build_admin_parser
from dish_tool.constants import EXIT_STATUS_BY_CODE
from dish_tool.database import initialize_database

ROOT = Path(__file__).resolve().parents[1]
ACTIVATION = ROOT / "docs" / "runtime-contract.md"
RELAY = ROOT / "docs" / "dish-chatgpt-relay.md"
REPORTS = ROOT / "dish-reports.sql"


def test_documented_agent_examples_match_current_parser_surface():
    parser = build_parser()
    examples = [
        ["sections", "--agent", "gpt"],
        ["create", "--agent", "gpt", "--title", "Dish"],
        ["read", "123", "--agent", "gpt"],
        ["inspect", "op", "--agent", "gpt"],
        ["start", "123", "--agent", "gpt", "--kind", "planning"],
        ["prepare", "op", "--agent", "gpt", "--model", "gpt-5.6-sol", "--file", "candidate.md"],
        [
            "approve", "op", "--agent", "gpt", "--model", "gpt-5.6-sol", "--correction", "none",
            "--semantic-review-complete", "--provenance-complete",
        ],
        ["reject", "op", "--agent", "gpt", "--reason", "reason"],
        ["submit", "op"],
    ]
    for argv in examples:
        assert vars(parser.parse_args(argv))["command"] == argv[0]


def test_documented_admin_examples_match_current_parser_surface():
    parser = build_admin_parser()
    examples = [
        ["migrate", "123"],
        ["recover", "op", "--outcome", "applied", "--reason", "reread"],
        ["discard", "op", "--reason", "abandoned"],
        ["resolved", "op"],
        [
            "reopen", "op", "--category", "method", "--before", "old",
            "--after", "new", "--editor", "codex", "--model", "gpt-5.6-sol",
            "--run-id", "editor-run",
            "--file", "corrected.md", "--date", "2026-07-25",
        ],
    ]
    for argv in examples:
        assert vars(parser.parse_args(argv))["command"] == argv[0]


def test_activation_has_guidance_for_every_result_code_and_exit_status():
    text = ACTIVATION.read_text(encoding="utf-8")
    for code, status in EXIT_STATUS_BY_CODE.items():
        assert f"`{code}`" in text
        assert re.search(rf"\| `{re.escape(code)}` \| {status} \|", text)
    for required in (
        "Tool pass:",
        "Agent-correctable finding:",
        "Possible Evidence or Human Review:",
        "Execution error or ambiguous result:",
        "Tool/protocol disagreement:",
        "The protocol wins",
        "single-agent local test path",
    ):
        assert required in text


def _report_queries(text: str) -> list[str]:
    return [
        match.group(2).strip()
        for match in re.finditer(
            r"-- report: ([^\n]+)\n(.*?)\n-- end report", text, re.S
        )
    ]


def test_all_step10_reports_execute_against_current_empty_schema(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    try:
        queries = _report_queries(REPORTS.read_text(encoding="utf-8"))
        assert len(queries) >= 10
        for query in queries:
            conn.execute(query).fetchall()
    finally:
        conn.close()


def test_reopen_planning_admin_cli_contract():
    from dish_tool.admin_cli import build_parser
    parsed = build_parser().parse_args([
        "reopen-planning", "task-gid", "--reason", "cook again"
    ])
    assert parsed.command == "reopen-planning"
    assert parsed.task_gid == "task-gid"
    assert parsed.reason == "cook again"
