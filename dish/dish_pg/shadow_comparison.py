"""Comparison-only normalization for dark-launch evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def semantic_normalizer(value: Mapping[str, Any]) -> Any:
    """Normalize only transport/replay metadata, never workflow semantics."""

    def clean(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): clean(child)
                for key, child in item.items()
                if key
                not in {
                    "request_replayed",
                    "captured_at",
                    "service_cleanup_warning",
                }
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    return clean(value)
