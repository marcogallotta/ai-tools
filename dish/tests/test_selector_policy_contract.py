from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_completion_does_not_imply_an_unconditional_full_suite() -> None:
    root_guidance = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Run the ordinary full suite before final delivery of a completed change block." not in root_guidance


def test_testing_policy_uses_tracked_git_delta_as_selector_authority() -> None:
    testing_policy = (REPO_ROOT / "dish" / "docs" / "testing.md").read_text(encoding="utf-8")
    normalized = testing_policy.lower().replace("-", " ")
    assert "git tracked" in normalized
    assert "complete" in normalized and "delta" in normalized
