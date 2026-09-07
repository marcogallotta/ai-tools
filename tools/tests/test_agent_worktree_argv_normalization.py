from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from agent_worktree_lib.argv_normalization import propagate_takeover_to_wrapped_resume
from agent_worktree_lib.cli import build_parser


def test_claim_takeover_is_propagated_to_wrapped_resume() -> None:
    argv = [
        "tools/agent-worktree",
        "claim",
        "--task",
        "1218222402374730",
        "--branch",
        "agent/example",
        "--agent-id",
        "replacement",
        "--takeover",
        "--expected-claim",
        "old-token",
        "--",
        "python3",
        "/repo/tools/agent-worktree",
        "resume",
        "--task",
        "1218222402374730",
        "--agent-id",
        "replacement",
    ]

    normalized = propagate_takeover_to_wrapped_resume(argv)
    args = build_parser().parse_args(normalized[1:])

    assert args.takeover is True
    resume_index = args.argv.index("resume")
    assert args.argv[resume_index + 1] == "--takeover"


def test_non_takeover_claim_and_unrelated_child_are_unchanged() -> None:
    plain = ["tools/agent-worktree", "claim", "--task", "1", "--branch", "agent/x", "--agent-id", "a", "--", "python3", "-c", "pass"]
    assert propagate_takeover_to_wrapped_resume(plain) == plain

    unrelated = ["tools/agent-worktree", "claim", "--task", "1", "--branch", "agent/x", "--agent-id", "a", "--takeover", "--expected-claim", "old", "--", "python3", "resume", "job"]
    assert propagate_takeover_to_wrapped_resume(unrelated) == unrelated
