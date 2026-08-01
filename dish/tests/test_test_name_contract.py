from __future__ import annotations

import ast
import re
from pathlib import Path


_VAGUE_CONFIDENCE_CLAIMS = re.compile(
    r"(?:^|_)(?:works|works_correctly|handles_everything|remains_intact|is_correct)(?:_|$)"
)


def test_test_names_describe_observed_behavior_without_vague_confidence_claims():
    offenders = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            if _VAGUE_CONFIDENCE_CLAIMS.search(node.name):
                offenders.append(f"{path.name}::{node.name}")
    assert offenders == [], (
        "test names must state the concrete state, result, or boundary asserted "
        f"instead of a broad confidence claim: {offenders}"
    )
