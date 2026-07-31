from __future__ import annotations

import re
from pathlib import Path


_HISTORICAL_PATTERNS = {
    "numbered-ticket": re.compile(r"^test_dish_\d{3}[a-z]?_"),
    "requirement-ticket": re.compile(r"_r\d+(?:_r?\d+)*_"),
    "audit-ticket": re.compile(r"^test_dish_audit_\d+_"),
    "review-history": re.compile(r"(?:hardening|easy_backlog|review_fixes|characterization|_gate)"),
}


def test_test_modules_are_named_for_stable_owned_behavior():
    offenders: list[str] = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        for label, pattern in _HISTORICAL_PATTERNS.items():
            if pattern.search(path.name):
                offenders.append(f"{path.name} ({label})")
    assert offenders == [], (
        "test filenames must describe stable owned behavior rather than ticket, "
        f"review, or backlog history: {offenders}"
    )
