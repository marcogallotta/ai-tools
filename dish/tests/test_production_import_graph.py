"""Production modules must have an explicit acyclic dependency direction."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {"dish_tool", "dish_service"}


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _resolve_relative(current: str, node: ast.ImportFrom) -> str | None:
    package = current.split(".")[:-1]
    if node.level:
        keep = len(package) - (node.level - 1)
        if keep < 0:
            return None
        package = package[:keep]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _graph() -> dict[str, set[str]]:
    paths = [
        path
        for package in PACKAGES
        for path in (ROOT / package).rglob("*.py")
        if path.name != "__init__.py"
    ]
    modules = {_module_name(path): path for path in paths}
    graph = {module: set() for module in modules}
    for module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_relative(module, node)
                if resolved:
                    targets.append(resolved)
            for target in targets:
                candidates = [target]
                candidates.extend(
                    name for name in modules if name.startswith(target + ".")
                )
                for candidate in candidates:
                    if candidate in modules and candidate != module:
                        graph[module].add(candidate)
    return graph


def _cycles(graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
    visiting: list[str] = []
    active: set[str] = set()
    done: set[str] = set()
    found: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in active:
            start = visiting.index(node)
            cycle = visiting[start:]
            rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
            found.add(min(rotations))
            return
        active.add(node)
        visiting.append(node)
        for target in sorted(graph[node]):
            visit(target)
        visiting.pop()
        active.remove(node)
        done.add(node)

    for node in sorted(graph):
        visit(node)
    return sorted(found)


def test_production_import_graph_is_acyclic():
    assert _cycles(_graph()) == []


def test_numbered_workflow_dependencies_point_to_earlier_stages_only():
    graph = _graph()
    numbered = {f"dish_tool.step{number}" for number in range(5, 10)}
    offenders: dict[str, list[str]] = {}
    for module, targets in graph.items():
        if module not in numbered:
            continue
        source_number = int(module.rsplit("step", 1)[1])
        invalid = [
            target
            for target in sorted(targets)
            if target in numbered
            and int(target.rsplit("step", 1)[1]) >= source_number
        ]
        if invalid:
            offenders[module] = invalid
    assert offenders == {}


def test_cross_stage_imports_do_not_use_private_stage_helpers():
    offenders = []
    for path in (ROOT / "dish_tool").glob("step[5-9].py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("step"):
                continue
            private = [alias.name for alias in node.names if alias.name.startswith("_")]
            if private:
                offenders.append((path.name, node.module, private))
    assert offenders == []
