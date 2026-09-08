"""Natural-language destination selection shared by fast-track entry points."""
from __future__ import annotations

import re

from .common import fail

_FAST_TRACK_TRIGGER_RE = re.compile(r"\b(?:fastrack|fast(?:[\s-]?track))\b", re.IGNORECASE)
_DIRECT_MAIN_RE = re.compile(r"\b(?:right|direct(?:ly)?)\s+to\s+main\b", re.IGNORECASE)


def classify_fast_track_words(words: str) -> str:
    """Return the named route; never treat the generic label as PR by default."""
    if not isinstance(words, str) or (
        _FAST_TRACK_TRIGGER_RE.search(words) is None and _DIRECT_MAIN_RE.search(words) is None
    ):
        fail("FAST_TRACK_ROUTE_REQUIRED", "words do not contain a fast-track trigger or a direct-to-main route")
    normalized = " ".join(words.lower().replace("-", " ").split())
    routes = set()
    if re.search(r"\b(?:to|right to|direct(?:ly)? to) main\b", normalized):
        routes.add("main")
    if re.search(r"\bto (?:pr|pull request)\b", normalized):
        routes.add("pr")
    if re.search(r"\bto (?:testing|test)\b", normalized):
        routes.add("testing")
    if len(routes) > 1:
        fail("FAST_TRACK_ROUTE_AMBIGUOUS", f"fast-track words name multiple destinations: {sorted(routes)}")
    return next(iter(routes), "recommend")
