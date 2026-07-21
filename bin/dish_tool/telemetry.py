"""Deterministic, content-free summaries of accepted change candidates."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence


def _canonical_headings(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    headings = manifest.get("headings")
    if not isinstance(headings, Mapping):
        return ()
    allowed = headings.get("allowed")
    if not isinstance(allowed, Sequence) or isinstance(allowed, (str, bytes)):
        return ()
    return tuple(str(item) for item in allowed if str(item))


def _line_heading_contexts(
    lines: Sequence[str], canonical_headings: Sequence[str]
) -> tuple[str | None, ...]:
    heading_set = set(canonical_headings)
    current: str | None = None
    contexts: list[str | None] = []
    for line in lines:
        if line in heading_set:
            current = line
        contexts.append(current)
    return tuple(contexts)


def calculate_change_diff(
    live_notes: str,
    candidate_notes: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Return compact diff telemetry without retaining either source document.

    Character counts use Python Unicode code points, including line terminators.
    Line counts follow ordinary diff semantics: a modified line is one removal and
    one addition. Heading attribution is the union of canonical heading contexts
    containing added, removed, or replaced lines.
    """

    char_added = 0
    char_removed = 0
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
        None, live_notes, candidate_notes, autojunk=False
    ).get_opcodes():
        if tag in {"replace", "delete"}:
            char_removed += old_end - old_start
        if tag in {"replace", "insert"}:
            char_added += new_end - new_start

    old_lines = live_notes.splitlines()
    new_lines = candidate_notes.splitlines()
    canonical_headings = _canonical_headings(manifest)
    old_contexts = _line_heading_contexts(old_lines, canonical_headings)
    new_contexts = _line_heading_contexts(new_lines, canonical_headings)

    lines_added = 0
    lines_removed = 0
    changed_headings: set[str] = set()
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        if tag in {"replace", "delete"}:
            lines_removed += old_end - old_start
            changed_headings.update(
                heading
                for heading in old_contexts[old_start:old_end]
                if heading is not None
            )
        if tag in {"replace", "insert"}:
            lines_added += new_end - new_start
            changed_headings.update(
                heading
                for heading in new_contexts[new_start:new_end]
                if heading is not None
            )

    ordered_headings = [
        heading for heading in canonical_headings if heading in changed_headings
    ]
    return {
        "characters_added": char_added,
        "characters_removed": char_removed,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "headings_changed": ordered_headings,
    }
