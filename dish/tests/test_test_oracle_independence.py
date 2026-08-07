from __future__ import annotations

import ast
from pathlib import Path

import pytest


# dish_service.command_spec is the single typed descriptive source for command
# identity/classification (stage A3 consolidation, commit 33e18e2). It is not a
# behavioral implementation module: action_contract.py may read its typed
# constants without losing independence from the production code paths it
# actually oracle-checks (command handling, transport, workflow behavior).
_APPROVED_DESCRIPTIVE_SOURCES = {
    "action_contract.py": {"dish_service.command_spec"},
}


@pytest.mark.parametrize(
    "support_name", ["action_contract.py", "recovery_fixture_contract.py"]
)
def test_independent_contract_support_does_not_import_production_contracts(support_name):
    path = Path(__file__).parent / "support" / support_name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    production_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            production_imports.extend(
                alias.name for alias in node.names if alias.name.startswith(("dish_service", "dish_tool"))
            )
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            ("dish_service", "dish_tool")
        ):
            production_imports.append(node.module or "")

    approved = _APPROVED_DESCRIPTIVE_SOURCES.get(support_name, set())
    unapproved_imports = [name for name in production_imports if name not in approved]
    assert unapproved_imports == []
