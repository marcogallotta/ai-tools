from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_repository_claude_uses_dish_operator_output_style():
    settings = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    assert settings["outputStyle"] == "Dish Operator"
    style = (ROOT / ".claude/output-styles/dish-operator.md").read_text(encoding="utf-8")
    assert "name: Dish Operator" in style
    assert "keep-coding-instructions: true" in style
    assert "Do not narrate routine investigation" in style
    assert "Marco operates at the level of outcomes" in style
    assert "STRESS MODE ACTIVATED" in style
    assert "Stress mode changes communication and interruption threshold only" in style


def test_no_compound_bash_is_retired_from_repository():
    assert not (ROOT / "hooks/no-compound-bash").exists()
    assert not (ROOT / "hooks/tests/test_no_compound_bash.py").exists()
    assert "no-compound-bash" not in (ROOT / "hooks/test-nudge.sh").read_text(encoding="utf-8")
