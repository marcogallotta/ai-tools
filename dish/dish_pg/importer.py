"""One-shot importer CLI: drives CoreAuthorityService.import_task_document.

Assumption: source data is newline-delimited JSON (NDJSON), with one object per
``ImportedTaskSpec``. This module uses the existing ``CoreAuthorityService`` and
never reimplements import authority. Repository-specific import-run preparation,
idempotency lookup, and session construction are injected as ``module:function``
callables.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from dish_pg.repositories import CoreAuthorityError
from dish_pg.services import (
    CoreAuthorityService,
    ImportedOperationHistorySpec,
    ImportedOperationRunRevocationSpec,
    ImportedServiceLeaseSpec,
    ImportedTaskResult,
    ImportedTaskSpec,
    ImportedVerificationCycleSpec,
    ImportedWorkflowOperationSpec,
)

LOGGER = logging.getLogger(__name__)


class SessionLike(Protocol):
    def __enter__(self) -> "SessionLike": ...
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    def begin(self) -> Any: ...


SessionFactory = Callable[[], SessionLike]
PrepareImportRun = Callable[[SessionLike, UUID, UUID, UUID], None]
AlreadyImported = Callable[[SessionLike, UUID], bool]
ServiceFactory = Callable[[SessionLike], CoreAuthorityService]


@dataclass(frozen=True)
class ImportSummary:
    imported: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


@dataclass(frozen=True)
class SourceRecord:
    line_number: int
    identifier: str
    spec: ImportedTaskSpec | None
    error: str | None = None


def _load_callable(path: str) -> Callable[..., Any]:
    module_name, separator, attribute = path.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"callable must use module:function syntax: {path!r}")
    value = getattr(importlib.import_module(module_name), attribute)
    if not callable(value):
        raise TypeError(f"configured object is not callable: {path!r}")
    return value


def _parse_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def _required_string(record: Mapping[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _optional_string(record: Mapping[str, object], field: str) -> str | None:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _optional_uuid(record: Mapping[str, object], field: str) -> UUID | None:
    value = record.get(field)
    return None if value in {None, ""} else UUID(str(value))


def _optional_datetime(record: Mapping[str, object], *, field: str) -> datetime | None:
    value = record.get(field)
    return None if value is None else _parse_datetime(value, field=field)


def _optional_positive_int(record: Mapping[str, object], field: str) -> int | None:
    value = record.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer or null")
    return value


def _history_items(history: Mapping[str, object], field: str) -> list[Mapping[str, object]]:
    if field not in history:
        raise ValueError(f"operation_history.{field} is required; re-export the legacy source")
    value = history[field]
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"operation_history.{field} must be a JSON array of objects")
    return list(value)


def operation_history_from_mapping(record: Mapping[str, object]) -> ImportedOperationHistorySpec:
    value = record.get("operation_history")
    if not isinstance(value, Mapping):
        raise ValueError("operation_history is required; re-export the legacy source")
    operations = tuple(
        ImportedWorkflowOperationSpec(
            operation_id=UUID(_required_string(item, "operation_id")),
            kind=_required_string(item, "kind"),
            status=_required_string(item, "status"),
            phase=_required_string(item, "phase"),
            terminal_outcome=_optional_string(item, "terminal_outcome"),
            created_at=_parse_datetime(item.get("created_at"), field="operation_history.operations.created_at"),
            completed_at=_optional_datetime(item, field="completed_at"),
        )
        for item in _history_items(value, "operations")
    )
    leases = tuple(
        ImportedServiceLeaseSpec(
            lease_id=UUID(_required_string(item, "lease_id")),
            operation_id=UUID(_required_string(item, "operation_id")),
            source_run_id=_required_string(item, "source_run_id"),
            owner_id=_required_string(item, "owner_id"),
            lease_kind=_required_string(item, "lease_kind"),
            actor_attempt_sequence=_optional_positive_int(item, "actor_attempt_sequence"),
            verification_cycle_id=_optional_uuid(item, "verification_cycle_id"),
            issued_at=_parse_datetime(item.get("issued_at"), field="operation_history.leases.issued_at"),
            expires_at=_parse_datetime(item.get("expires_at"), field="operation_history.leases.expires_at"),
            released_at=_optional_datetime(item, field="released_at"),
        )
        for item in _history_items(value, "leases")
    )
    cycles = tuple(
        ImportedVerificationCycleSpec(
            cycle_id=UUID(_required_string(item, "cycle_id")),
            operation_id=UUID(_required_string(item, "operation_id")),
            cycle_sequence=_optional_positive_int(item, "cycle_sequence") or 0,
            outcome=_optional_string(item, "outcome"),
            created_at=_parse_datetime(item.get("created_at"), field="operation_history.verification_cycles.created_at"),
            completed_at=_optional_datetime(item, field="completed_at"),
        )
        for item in _history_items(value, "verification_cycles")
    )
    revocations = tuple(
        ImportedOperationRunRevocationSpec(
            revocation_id=UUID(_required_string(item, "revocation_id")),
            operation_id=UUID(_required_string(item, "operation_id")),
            owner_id=_required_string(item, "owner_id"),
            source_run_id=_required_string(item, "source_run_id"),
            source_lease_id=_optional_uuid(item, "source_lease_id"),
            reason=_required_string(item, "reason"),
            revoked_at=_parse_datetime(
                item.get("revoked_at"), field="operation_history.revocations.revoked_at"
            ),
        )
        for item in _history_items(value, "revocations")
    )
    return ImportedOperationHistorySpec(
        operations=operations,
        leases=leases,
        verification_cycles=cycles,
        revocations=revocations,
    )


def _spec_from_mapping(record: Mapping[str, object]) -> ImportedTaskSpec:
    projects = record.get("project_ids")
    if not isinstance(projects, list):
        raise ValueError("project_ids must be a JSON array")
    completed = record.get("completed")
    if not isinstance(completed, bool):
        raise ValueError("completed must be a boolean")
    return ImportedTaskSpec(
        task_id=UUID(_required_string(record, "task_id")),
        asana_task_gid=_required_string(record, "asana_task_gid"),
        title=_required_string(record, "title"),
        body=_required_string(record, "body"),
        identity_scheme=_required_string(record, "identity_scheme"),
        content_identity=_required_string(record, "content_identity"),
        project_ids=tuple(UUID(str(value)) for value in projects),
        section_id=UUID(_required_string(record, "section_id")),
        completed=completed,
        observed_at=_parse_datetime(record.get("observed_at"), field="observed_at"),
        operation_history=operation_history_from_mapping(record),
        existence_state=str(record.get("existence_state", "ordinary")),
    )


def iter_source(path: Path) -> Iterator[SourceRecord]:
    """Yield every NDJSON record without allowing one malformed line to hide later lines."""
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            identifier = f"line:{line_number}"
            try:
                value = json.loads(raw_line)
                if not isinstance(value, dict):
                    raise ValueError("record must be a JSON object")
                if isinstance(value.get("task_id"), str):
                    identifier = value["task_id"]
                yield SourceRecord(line_number, identifier, _spec_from_mapping(value))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                yield SourceRecord(line_number, identifier, None, str(exc))


def _log_result(result: ImportedTaskResult) -> None:
    LOGGER.info("imported %s", json.dumps({key: str(value) for key, value in asdict(result).items()}, sort_keys=True))


def run_import(
    *,
    source: Path,
    generation_id: UUID,
    import_run_id: UUID,
    contract_binding_id: UUID,
    session_factory: SessionFactory,
    prepare_import_run: PrepareImportRun,
    already_imported: AlreadyImported,
    service_factory: ServiceFactory = CoreAuthorityService,
) -> ImportSummary:
    """Prepare the run once, then import each source record in its own transaction."""
    with session_factory() as session:
        with session.begin():
            prepare_import_run(session, generation_id, import_run_id, contract_binding_id)

    imported = skipped = failed = 0
    for record in iter_source(source):
        if record.error is not None or record.spec is None:
            failed += 1
            LOGGER.error("import failed record=%s error=%s", record.identifier, record.error)
            continue
        try:
            with session_factory() as session:
                with session.begin():
                    if already_imported(session, record.spec.task_id):
                        skipped += 1
                        LOGGER.info("skipped already imported task_id=%s", record.spec.task_id)
                        continue
                    result = service_factory(session).import_task_document(
                        generation_id=generation_id,
                        import_run_id=import_run_id,
                        contract_binding_id=contract_binding_id,
                        spec=record.spec,
                    )
            imported += 1
            _log_result(result)
        except CoreAuthorityError as exc:
            failed += 1
            LOGGER.error("import failed record=%s error=%s", record.identifier, exc)
        except Exception:
            failed += 1
            LOGGER.exception("import failed record=%s unexpected error", record.identifier)

    summary = ImportSummary(imported=imported, skipped=skipped, failed=failed)
    LOGGER.info("summary imported=%d skipped=%d failed=%d", imported, skipped, failed)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-import")
    parser.add_argument("--source", required=True, type=Path, help="NDJSON file; one ImportedTaskSpec per line")
    parser.add_argument("--generation-id", required=True, type=UUID)
    parser.add_argument("--import-run-id", required=True, type=UUID)
    parser.add_argument("--contract-binding-id", required=True, type=UUID)
    parser.add_argument("--session-factory", required=True, help="module:function returning a new Session")
    parser.add_argument("--prepare-import-run", required=True, help="module:function precondition hook")
    parser.add_argument("--already-imported", required=True, help="module:function idempotency check")
    parser.add_argument("--service-factory", default="dish_pg.services:CoreAuthorityService")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(levelname)s %(message)s")
    try:
        summary = run_import(
            source=args.source,
            generation_id=args.generation_id,
            import_run_id=args.import_run_id,
            contract_binding_id=args.contract_binding_id,
            session_factory=_load_callable(args.session_factory),
            prepare_import_run=_load_callable(args.prepare_import_run),
            already_imported=_load_callable(args.already_imported),
            service_factory=_load_callable(args.service_factory),
        )
    except Exception:
        LOGGER.exception("importer setup failed")
        return 2
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
