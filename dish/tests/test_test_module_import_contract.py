from __future__ import annotations

import ast
from pathlib import Path


def _test_modules() -> set[str]:
    root = Path(__file__).parent
    return {
        path.stem
        for path in root.glob("test_*.py")
        if path.name != Path(__file__).name
    }


def test_tests_do_not_import_other_test_modules():
    """Shared helpers belong in tests.support, never another collected test module."""

    root = Path(__file__).parent
    test_modules = _test_modules()
    violations: list[str] = []

    for path in sorted(root.rglob("*.py")):
        if path == Path(__file__):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            for module in imported:
                top_level = module.split(".", 1)[0]
                if module.startswith("tests.test_") or top_level in test_modules:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: imports {module}"
                    )

    assert not violations, (
        "tests must import shared helpers from tests.support rather than another "
        "collected test module:\n" + "\n".join(violations)
    )
