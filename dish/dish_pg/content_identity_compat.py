"""Bounded dark-launch compatibility for historical PostgreSQL content identities."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

from dish_tool.content_versions import content_identity


def canonical_target_content_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Translate only a row that exactly proves the historical NUL serialization.

    Callers receive a copy. Unknown/corrupt identities and all title/body fields
    are preserved verbatim so genuine differences remain visible.
    """
    clean = dict(row)
    title = clean.get("title")
    body = clean.get("body")
    stored_identity = clean.get("identity")
    if not all(isinstance(value, str) for value in (title, body, stored_identity)):
        return clean
    old_identity = hashlib.sha256(f"{title}\0{body}".encode("utf-8")).hexdigest()
    if stored_identity == old_identity:
        clean["identity"] = content_identity(title, body)
    return clean
