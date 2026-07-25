"""Schema migration primitives for canonical dish task documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .task_document import CanonicalTaskDocument, DocumentFinding, FindingKind, parse_task_document, validate_task_document


@dataclass(frozen=True)
class MigrationResult:
    source_schema_version: str
    target_schema_version: str
    document: CanonicalTaskDocument | None
    transformed_content: str | None
    findings: tuple[DocumentFinding, ...]
    quarantined: bool = False

    @property
    def ok(self) -> bool:
        return not self.findings and not self.quarantined and self.document is not None


Transform = Callable[[CanonicalTaskDocument], CanonicalTaskDocument]


def migrate_task_document(content: str, migration: Mapping[str, Any], *, transform: Transform | None = None) -> MigrationResult:
    source = migration.get("from_schema_version")
    target = migration.get("to_schema_version")
    if not isinstance(source, str) or not source or not isinstance(target, str) or not target:
        finding = DocumentFinding("migration.metadata", FindingKind.SYNTAX, "migration requires string from/to schema versions")
        return MigrationResult(str(source or ""), str(target or ""), None, None, (finding,), True)
    try:
        document = parse_task_document(content)
    except ValueError as exc:
        finding = DocumentFinding("migration.ambiguous-legacy", FindingKind.SEMANTIC_REVIEW, str(exc))
        return MigrationResult(source, target, None, None, (finding,), True)
    if document.schema_version != source:
        finding = DocumentFinding("migration.source-version", FindingKind.SCHEMA_VERSION, f"task declares schema {document.schema_version}; migration requires {source}")
        return MigrationResult(source, target, document, None, (finding,), True)
    candidate = transform(document) if transform else document
    # The primitive intentionally does not claim/write the target Schema version.
    structural = validate_task_document(candidate, expected_schema_version=None)
    return MigrationResult(source, target, candidate, candidate.render(), structural.findings, bool(structural.findings))
