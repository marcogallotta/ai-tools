from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9_.-]+)")


def _declared_requirements(path: Path, *, seen: set[Path] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen:
        return set()
    seen.add(path)

    declared: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            included = line.split(maxsplit=1)[1]
            declared.update(_declared_requirements(path.parent / included, seen=seen))
            continue
        match = _REQUIREMENT_NAME.match(line)
        if match:
            declared.add(match.group(1).casefold().replace("-", "_"))
    return declared


def _direct_third_party_imports() -> set[str]:
    local_modules = {"dish_service", "dish_tool", "tests"}
    imported: set[str] = set()
    for test_file in TESTS.rglob("*.py"):
        tree = ast.parse(test_file.read_text(encoding="utf-8"), filename=str(test_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".", 1)[0]]
            else:
                continue
            for name in names:
                if (
                    name in sys.stdlib_module_names
                    or name in local_modules
                    or name.startswith("test_")
                ):
                    continue
                imported.add(name.casefold().replace("-", "_"))
    return imported


def test_all_direct_test_dependencies_are_declared():
    declared = _declared_requirements(ROOT / "requirements-test.txt")
    imported = _direct_third_party_imports()

    assert imported <= declared, (
        "tests import undeclared third-party modules: "
        f"{sorted(imported - declared)}"
    )
    assert {"asana", "pytest", "jsonschema"} <= declared
