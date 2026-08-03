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
from dish_pg.services import CoreAuthorityService, ImportedTaskResult, ImportedTaskSpec

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
