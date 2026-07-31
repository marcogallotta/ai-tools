from __future__ import annotations

import ast
from pathlib import Path


def test_tests_do_not_mutate_python_import_paths():
    root = Path(__file__).parent
    violations: list[str] = []

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if (
                    isinstance(owner, ast.Attribute)
                    and isinstance(owner.value, ast.Name)
                    and owner.value.id == "sys"
                    and owner.attr == "path"
                    and node.func.attr in {"append", "extend", "insert", "remove"}
                ):
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: mutates sys.path"
                    )
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    text = ast.unparse(target)
                    if text.startswith("sys.path"):
                        violations.append(
                            f"{path.relative_to(root)}:{node.lineno}: mutates sys.path"
                        )
                    if "PYTHONPATH" in text:
                        violations.append(
                            f"{path.relative_to(root)}:{node.lineno}: mutates PYTHONPATH"
                        )

    assert not violations, (
        "tests must import through the repository package layout rather than "
        "mutating sys.path or PYTHONPATH:\n" + "\n".join(violations)
    )
