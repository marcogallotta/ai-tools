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
        value = json.loads(Path(manifest).resolve().read_text(encoding="utf-8")).get(
            "source_commit"
        )
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


__all__ = ["executable_code_release"]


def process_code_identity() -> dict[str, object]:
    """Capture once during server construction, before serving requests."""
    own_manifest = Path(__file__).resolve().parents[2] / ".dish-release.json"
    code = executable_code_release()
    if own_manifest.exists():
        own_code = json.loads(own_manifest.read_text()).get("source_commit")
        code = own_code if own_code == code and _SHA.fullmatch(str(own_code)) else None
    return {
        "code_release": code,
        "pid": os.getpid(),
        "start_ticks": Path("/proc/self/stat")
        .read_text()
        .rsplit(")", 1)[1]
        .split()[19],
    }
