from __future__ import annotations

import ast
import hashlib
from collections import defaultdict
from pathlib import Path


def _normalized_test_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ast.dump(ast.Module(body=node.body, type_ignores=[]), include_attributes=False)


def test_no_exact_test_body_is_duplicated_across_modules():
    tests_root = Path(__file__).parent
    by_digest: dict[str, list[str]] = defaultdict(list)
    for path in sorted(tests_root.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                digest = hashlib.sha256(_normalized_test_body(node).encode("utf-8")).hexdigest()
                by_digest[digest].append(f"{path.name}::{node.name}")
    duplicates = [
        locations for locations in by_digest.values()
        if len({location.split("::", 1)[0] for location in locations}) > 1
    ]
    assert duplicates == [], (
        "exact test bodies are duplicated across modules; keep one authoritative "
        f"owner or parametrize the shared behavior: {duplicates}"
    )


def _normalized_helper(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    normalized = ast.parse(ast.unparse(node)).body[0]
    normalized.name = "_helper"
    return ast.dump(normalized, include_attributes=False)


def test_no_substantial_helper_is_duplicated_across_test_modules():
    tests_root = Path(__file__).parent
    by_digest: dict[str, list[str]] = defaultdict(list)
    for path in sorted(tests_root.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("test_"):
                continue
            line_count = node.end_lineno - node.lineno + 1
            if line_count < 5 or len(node.body) < 3:
                continue
            digest = hashlib.sha256(_normalized_helper(node).encode("utf-8")).hexdigest()
            by_digest[digest].append(f"{path.name}::{node.name}")
    duplicates = [
        locations for locations in by_digest.values()
        if len({location.split("::", 1)[0] for location in locations}) > 1
    ]
    assert duplicates == [], (
        "substantial test setup is duplicated across modules; move semantically "
        f"important setup into tests/support: {duplicates}"
    )
