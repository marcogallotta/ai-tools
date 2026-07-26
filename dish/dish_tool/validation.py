"""Compatibility facade for dish validation modules."""

from .content_validation import (
    extract_exact_label_line,
    parse_canonical_title,
    render_title,
    validate_note,
    validate_title_declaration,
)
from .schema_validation import validate_manifest_shape, validate_task_schema_shape

__all__ = [
    "extract_exact_label_line",
    "parse_canonical_title",
    "render_title",
    "validate_manifest_shape",
    "validate_note",
    "validate_task_schema_shape",
    "validate_title_declaration",
]
