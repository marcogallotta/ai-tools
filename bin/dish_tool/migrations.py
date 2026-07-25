"""Executable, fail-closed schema migrations for canonical dish task documents."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

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


OperationHandler = Callable[[CanonicalTaskDocument, Mapping[str, Any], str], CanonicalTaskDocument]


def _canonical_parse_render(document: CanonicalTaskDocument, operation: Mapping[str, Any], target: str) -> CanonicalTaskDocument:
    """Canonical schema migration: preserve facts and change only declared schema."""
    return dataclasses.replace(document, schema_version=target)


def _manual_reconciliation(document: CanonicalTaskDocument, operation: Mapping[str, Any], target: str) -> CanonicalTaskDocument:
    raise ValueError("manual-reconciliation migrations cannot be executed automatically")


_OPERATION_HANDLERS: Mapping[str, OperationHandler] = {
    "canonical-parse-render": _canonical_parse_render,
    "manual-reconciliation": _manual_reconciliation,
}


def _migration_operations(migration: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    operations = migration.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("migration requires a non-empty operations list")
    if not all(isinstance(item, Mapping) for item in operations):
        raise ValueError("migration operations must be objects")
    return operations


def migrate_task_document(
    content: str,
    migration: Mapping[str, Any],
    *,
    schema: Mapping[str, Any] | None = None,
) -> MigrationResult:
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
    candidate = document
    try:
        for operation in _migration_operations(migration):
            operation_type = operation.get("type")
            if not isinstance(operation_type, str) or operation_type not in _OPERATION_HANDLERS:
                raise ValueError(f"unsupported migration operation: {operation_type!r}")
            candidate = _OPERATION_HANDLERS[operation_type](candidate, operation, target)
    except ValueError as exc:
        finding = DocumentFinding("migration.operation", FindingKind.SEMANTIC_REVIEW, str(exc))
        return MigrationResult(source, target, document, None, (finding,), True)
    if candidate.schema_version != target:
        finding = DocumentFinding("migration.target-version", FindingKind.SCHEMA_VERSION, f"migration did not produce target schema {target}")
        return MigrationResult(source, target, candidate, None, (finding,), True)
    structural = validate_task_document(candidate, expected_schema_version=target, schema=schema)
    return MigrationResult(source, target, candidate, candidate.render(), structural.findings, bool(structural.findings))
