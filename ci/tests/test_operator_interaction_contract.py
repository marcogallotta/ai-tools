from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "dish" / "docs" / "agents" / "index.md"
POLICY = ROOT / "OPERATOR_CONTROL_PLANE.md"


def test_role_index_routes_every_role_through_one_shared_operator_contract():
    index = INDEX.read_text()
    assert "OPERATOR_CONTROL_PLANE.md" in index
    assert "not role composition or a new authority layer" in index
    text = POLICY.read_text()
    assert "## Shared operator interaction" in text


def test_operator_contract_is_presentation_only_and_preserves_intent_firewall():
    text = POLICY.read_text()
    assert "EXECUTE is not mutation authority" in text
    assert "explicit-intent policy" in text
    assert "only the exact previously established scope" in text
    assert "presentation only" in text
    assert "authoritative readback" in text


def test_operator_contract_keeps_action_first_and_attention_gate():
    text = POLICY.read_text()
    assert "say-do" in text
    assert "answer-first" in text
    assert "substantive operator-attention gate" in text
    assert "suppress standalone acknowledgements" in text
    for label in (
        "COMPLETE / LANDED",
        "CONTINUE AUTOMATICALLY",
        "FIX REQUIRED",
        "WAITING",
        "MANUAL ACTION",
        "HUMAN DECISION",
        "TRUE BLOCKER",
    ):
        assert label in text
