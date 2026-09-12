"""Production modules must have an explicit acyclic dependency direction."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {"dish_tool", "dish_service", "dish_pg"}

# Existing reverse dependencies are explicit debt, not permission for new ones.
# Stale entries are harmless: later cleanup may remove an edge without first editing
# this allowlist, while any newly introduced edge still fails the ratchet.
SERVICE_DEPENDENCY_DEBT = frozenset(
    {
        ("dish_tool.admin", "dish_service.leases"),
        ("dish_tool.admin", "dish_service.request_replay"),
        ("dish_pg.command_contract", "dish_service.command_spec"),
        ("dish_pg.cutover_control", "dish_service.legacy_writer_fence"),
        ("dish_pg.dark_launch", "dish_service.path_safety"),
        ("dish_pg.dark_launch", "dish_service.shadow_spool"),
        ("dish_pg.dark_launch_readiness", "dish_service.config"),
        ("dish_pg.dark_launch_readiness", "dish_service.path_safety"),
        ("dish_pg.dark_launch_readiness", "dish_service.shadow_spool"),
        ("dish_pg.postgres_service", "dish_service.leases"),
        ("dish_pg.shadow_worker", "dish_service.path_safety"),
        ("dish_pg.shadow_worker", "dish_service.shadow_spool"),
        ("dish_pg.test_discovery_qualifier", "dish_service.client"),
        ("dish_pg.test_journey_qualifier", "dish_service.client"),
    }
)


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _resolve_from_base(current: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module

    package = current.split(".")[:-1]
    keep = len(package) - (node.level - 1)
    if keep < 0:
        return None
    package = package[:keep]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _import_targets(
    current: str,
    node: ast.Import | ast.ImportFrom,
    modules: set[str],
) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names if alias.name in modules}

    base = _resolve_from_base(current, node)
    if not base:
        return set()

    targets = {base} if base in modules else set()
    for alias in node.names:
        if alias.name == "*":
            continue
        candidate = f"{base}.{alias.name}"
        if candidate in modules:
            targets.add(candidate)
    return targets


def _graph() -> dict[str, set[str]]:
    paths = [
        path
        for package in PACKAGES
        for path in (ROOT / package).rglob("*.py")
        if path.name != "__init__.py"
    ]
    module_paths = {_module_name(path): path for path in paths}
    modules = set(module_paths)
    graph = {module: set() for module in modules}
    for module, path in module_paths.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            graph[module].update(
                target
                for target in _import_targets(module, node, modules)
                if target != module
            )
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


def _service_dependency_edges(graph: dict[str, set[str]]) -> set[tuple[str, str]]:
    return {
        (module, target)
        for module, targets in graph.items()
        if module.startswith(("dish_tool.", "dish_pg."))
        for target in targets
        if target.startswith("dish_service.")
    }


def test_absolute_from_import_resolves_from_repository_root():
    modules = {"dish_tool.admin", "dish_service.leases"}
    node = ast.parse("from dish_service.leases import ServicePrincipal").body[0]
    assert isinstance(node, ast.ImportFrom)

    assert _import_targets("dish_tool.admin", node, modules) == {"dish_service.leases"}


def test_relative_package_import_resolves_only_named_submodule():
    modules = {"dish_pg.release", "dish_pg.models", "dish_pg.workflow"}
    node = ast.parse("from . import models").body[0]
    assert isinstance(node, ast.ImportFrom)

    assert _import_targets("dish_pg.release", node, modules) == {"dish_pg.models"}


def test_absolute_package_import_ignores_non_module_symbols():
    modules = {"dish_pg.release", "dish_pg.models"}
    node = ast.parse("from dish_pg import models, ALEMBIC_HEAD").body[0]
    assert isinstance(node, ast.ImportFrom)

    assert _import_targets("dish_pg.release", node, modules) == {"dish_pg.models"}


def test_production_import_graph_is_acyclic():
    assert _cycles(_graph()) == []


def test_tool_and_pg_do_not_add_service_dependencies():
    new_debt = _service_dependency_edges(_graph()) - SERVICE_DEPENDENCY_DEBT
    assert new_debt == set()


def test_dish_tool_does_not_depend_on_service_frontend_family():
    offenders: list[tuple[str, str]] = []
    for path in (ROOT / "dish_tool").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                targets = [node.module]
            else:
                continue
            for target in targets:
                if target == "dish_service.frontend" or target.startswith("dish_service.frontend_"):
                    offenders.append((str(path.relative_to(ROOT)), target))
    assert offenders == []


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
