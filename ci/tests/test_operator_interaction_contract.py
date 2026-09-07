from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "dish" / "docs" / "agents" / "index.md"
POLICY = ROOT / "OPERATOR_CONTROL_PLANE.md"
SOURCE = ROOT / "dish" / "docs" / "chatgpt-projects" / "source.json"
CLAUDE = ROOT / "CLAUDE.md"
CLAUDE_GLOBAL = ROOT / "CLAUDE-global.md"
CLAUDE_OPERATOR_STYLE = ROOT / ".claude" / "output-styles" / "dish-operator.md"
AGENTS = ROOT / "AGENTS.md"


def test_dish_role_index_is_the_loud_first_action_before_generic_skills_or_tools():
    for path in (CLAUDE, AGENTS):
        text = path.read_text()
        prefix = "\n".join(text.splitlines()[:12])
        assert "DISH REQUESTS: READ THE ROLE INDEX BEFORE ANYTHING ELSE" in prefix
        assert "FIRST Dish action" in prefix
        assert "dish/docs/agents/index.md" in prefix
        assert "/code-review" in prefix
        assert "role routing happens first" in prefix
        assert "mapped standing contract" in prefix


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
    assert "<!-- BEGIN GENERATED CHATTY WORK CONTRACT -->" in root
    assert "<!-- END GENERATED CHATTY WORK CONTRACT -->" in root
    for rule in rules:
        assert f"- {rule}" in root
    for brittle in ("give handoff", "YES OR NO", "speak in actions"):
        assert brittle not in "\n".join(rules)


def test_attention_contract_keeps_depth_and_minimum_packet_in_one_generated_source():
    rules = json.loads(SOURCE.read_text())["chatty_contract"]
    text = "\n".join(rules)
    assert "50%, 100%, or 200%" in text
    assert "Every depth retains:" in text
    assert "not truth, authority, completion" in text
    style = CLAUDE_OPERATOR_STYLE.read_text()
    assert "not an independent communication authority" in style
    for rule in rules:
        assert f"- {rule}" in style


def test_named_governed_task_preserves_required_asana_persistence_without_widening_ad_hoc_reads():
    global_text = CLAUDE_GLOBAL.read_text()
    mutation_policy = global_text.split("## Communication", 1)[0]
    asana_policy = global_text.split("## Asana write safety", 1)[1]
    index = INDEX.read_text()

    assert "`research task X`" in mutation_policy
    assert "required owning-task Asana persistence" in mutation_policy
    assert "`research this text` is read-only" in mutation_policy
    assert "generic/ad-hoc" in mutation_policy
    assert "An explicit assignment to perform a governed workflow authorizes the Asana writes" in asana_policy
    assert "without another chat confirmation" in asana_policy
    assert "bounded Asana persistence" in index
