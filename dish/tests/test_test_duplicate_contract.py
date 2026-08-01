from __future__ import annotations

import ast
import hashlib
from collections import defaultdict
from pathlib import Path


def _normalized_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> str:
    normalized = ast.parse(ast.unparse(node)).body[0]
    normalized.name = "_symbol"
    return ast.dump(normalized, include_attributes=False)


def _duplicates(by_digest: dict[str, list[str]]) -> list[list[str]]:
    return [
        locations
        for locations in by_digest.values()
        if len({location.split("::", 1)[0] for location in locations}) > 1
    ]


def test_no_exact_test_body_is_duplicated_across_modules():
    tests_root = Path(__file__).parent
    by_digest: dict[str, list[str]] = defaultdict(list)
    for path in sorted(tests_root.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                digest = hashlib.sha256(_normalized_symbol(node).encode("utf-8")).hexdigest()
                by_digest[digest].append(
                    f"{path.relative_to(tests_root)}::{node.name}"
                )
    duplicates = _duplicates(by_digest)
    assert duplicates == [], (
        "exact test bodies are duplicated across modules; keep one authoritative "
        f"owner or parametrize the shared behavior: {duplicates}"
    )


def test_no_substantial_helper_or_fake_class_is_duplicated():
    tests_root = Path(__file__).parent
    functions: dict[str, list[str]] = defaultdict(list)
    classes: dict[str, list[str]] = defaultdict(list)
    for path in sorted(tests_root.rglob("*.py")):
        if path == Path(__file__):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    continue
                line_count = node.end_lineno - node.lineno + 1
                if line_count < 5 or len(node.body) < 3:
                    continue
                digest = hashlib.sha256(_normalized_symbol(node).encode("utf-8")).hexdigest()
                functions[digest].append(
                    f"{path.relative_to(tests_root)}::{node.name}"
                )
            elif isinstance(node, ast.ClassDef):
                line_count = node.end_lineno - node.lineno + 1
                if line_count < 3:
                    continue
                digest = hashlib.sha256(_normalized_symbol(node).encode("utf-8")).hexdigest()
                classes[digest].append(
                    f"{path.relative_to(tests_root)}::{node.name}"
                )

    duplicates = _duplicates(functions) + _duplicates(classes)
    assert duplicates == [], (
        "test setup or fake behavior is duplicated across modules; move one "
        f"authoritative implementation into tests/support: {duplicates}"
    )
