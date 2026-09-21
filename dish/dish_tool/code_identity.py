"""Public executable identity exposed separately from database generation identity."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{40}")


def executable_code_release() -> str | None:
    manifest = os.environ.get("DISH_RELEASE_MANIFEST")
    if not manifest:
        return None
    try:
        value = json.loads(Path(manifest).read_text(encoding="utf-8")).get(
            "source_commit"
        )
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


__all__ = ["executable_code_release"]
