from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_worktree_lib.common import AgentWorktreeError  # noqa: E402
from agent_worktree_lib.fast_track_auth import classify_fast_track_words  # noqa: E402


@pytest.mark.parametrize(
    ("words", "route"),
    [
        ("fastrack to main", "main"),
        ("FAST-TRACK right to main NOW", "main"),
        ("fast track directly to main", "main"),
        ("right to main NOW", "main"),
        ("directly to main", "main"),
        ("fastrack to PR", "pr"),
        ("fast-track to pull request", "pr"),
        ("fastrack to testing", "testing"),
        ("fast track this", "recommend"),
    ],
)
def test_destination_dominates_fast_track_spelling(words: str, route: str):
    assert classify_fast_track_words(words) == route


def test_multiple_destinations_fail_instead_of_defaulting_to_pr():
    with pytest.raises(AgentWorktreeError) as exc:
        classify_fast_track_words("fastrack to PR and to main")
    assert exc.value.code == "FAST_TRACK_ROUTE_AMBIGUOUS"
