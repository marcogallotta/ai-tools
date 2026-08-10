"""Canonical Dish frontend URL parsing shared by human administration."""
from __future__ import annotations

from urllib.parse import urlsplit

from .errors import DishRuleError
from .identifiers import require_dish_uuid


def dish_uuid_from_url(value: str) -> str:
    """Extract the authoritative UUID from ``/dishes/<uuid>/<decorative-slug>``.

    The slug is deliberately non-authoritative.  Query strings and fragments are
    rejected so a copied frontend URL cannot smuggle additional target syntax.
    """
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.query or parsed.fragment:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Dish URL must not include a query string or fragment",
            rule="dish_url_invalid",
        )
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Dish URL must use http or https",
            rule="dish_url_invalid",
        )
    if parsed.scheme and not parsed.netloc:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Dish URL must include a host",
            rule="dish_url_invalid",
        )
    parts = parsed.path.split("/")
    if len(parts) != 4 or parts[0] != "" or parts[1] != "dishes" or not parts[3]:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Dish URL must match /dishes/<uuid>/<decorative-title-slug>",
            rule="dish_url_invalid",
        )
    if len(parts[3]) > 600:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Dish URL decorative title slug is too long",
            rule="dish_url_invalid",
        )
    return require_dish_uuid(parts[2], field="dish_id")
