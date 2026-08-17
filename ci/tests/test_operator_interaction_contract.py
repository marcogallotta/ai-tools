from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "dish" / "docs" / "agents" / "index.md"
POLICY = ROOT / "OPERATOR_CONTROL_PLANE.md"
SOURCE = ROOT / "dish" / "docs" / "chatgpt-projects" / "source.json"
CLAUDE = ROOT / "CLAUDE.md"
CLAUDE_STYLE = ROOT / ".claude" / "output-styles" / "dish-operator.md"


def test_role_index_routes_every_role_through_one_shared_operator_contract():
    index = INDEX.read_text()
    assert "OPERATOR_CONTROL_PLANE.md" in index
    assert "not role composition or a new authority layer" in index
    assert "## Shared operator interaction" in POLICY.read_text()


def test_operator_contract_is_authority_readback_only_and_preserves_intent_firewall():
    text = POLICY.read_text()
    assert "generic work-chat behavior is sourced once" in text.lower()
    assert "never creates mutation authority" in text
    assert "explicit mutation intent" in text
    assert "only the exact previously established scope" in text
    assert "authoritative readback" in text
    assert "progress message is not completion" in text
    assert "say-do" not in text
    assert "answer-first" not in text
    assert "substantive operator-attention gate" not in text


def test_canonical_chatty_contract_is_generated_into_root_and_avoids_phrase_dictionary():
    source = json.loads(SOURCE.read_text())
    rules = source["chatty_contract"]
    root = CLAUDE.read_text()
    assert len(rules) == 6
    assert "<!-- BEGIN GENERATED CHATTY WORK CONTRACT -->" in root
    assert "<!-- END GENERATED CHATTY WORK CONTRACT -->" in root
    style = CLAUDE_STYLE.read_text()
    for rule in rules:
        assert f"- {rule}" in root
        assert f"- {rule}" in style
    for brittle in ("YES OR NO", "one to three sentences", "1-3 short sentences"):
        assert brittle not in "\n".join(rules)
