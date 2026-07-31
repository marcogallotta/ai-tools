from __future__ import annotations

import ast
from pathlib import Path


def test_independent_contract_support_does_not_import_production_contracts():
    path = Path(__file__).parent / "support" / "action_contract.py"
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

    assert production_imports == []
