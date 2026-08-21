from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ROLE_INDEX = ROOT / "dish" / "docs" / "agents" / "index.md"


def test_sliced_pr_cannot_narrow_or_complete_governing_workstream():
    text = ROLE_INDEX.read_text(encoding="utf-8")

    required = [
        "A PR is a review/landing unit, not permission to redefine the governing assignment.",
        "MUST NOT narrow a full assignment into a smaller PR/slice",
        "Finishing, publishing, reviewing, or merging one member proves only that member's disposition.",
        "Review MUST BLOCK the scope drift rather than accepting rewritten PR prose as new authority.",
        "slice disposition",
        "workstream completion",
        "The next required slice remains active until the governing objective is actually complete.",
    ]

    for phrase in required:
        assert phrase in text


def test_local_completion_cannot_reinterpret_semantic_scope():
    text = ROLE_INDEX.read_text(encoding="utf-8")

    assert "A local publication/completion agent owns only exact publication/completion" in text
    assert "cannot reinterpret the semantic assignment" in text
    assert "declare remaining members future work without new durable scope authority" in text


def test_bundle_review_remains_required_when_governing_contract_requires_it():
    text = ROLE_INDEX.read_text(encoding="utf-8")

    assert "When the governing contract requires bundle/composition review" in text
    assert "Individual member reviews never substitute for that bundle verdict." in text
