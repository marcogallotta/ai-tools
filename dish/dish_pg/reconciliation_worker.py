"""Corpus reconciliation worker built on :class:`ProjectionService` authority.

The worker performs exactly one external operation: fetch a complete corpus for
one ``corpus_identity``.  That fetch completes before any authoritative database
transaction begins.  Inside one caller-owned transaction, a repository-specific
pure comparator translates each fetched item into the fields required by
``ProjectionService.record_reconciliation_item``; the worker then starts,
records, and completes one governed reconciliation run.

This module does not inspect or mutate projection outbox rows directly and does
not reimplement reconciliation state transitions.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
import signal
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Mapping, Protocol, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from . import stage5_models as tx
from .transition import ProjectionService

LOGGER = logging.getLogger("dish.reconciliation_worker")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _write_atomic(path: Path, value: object) -> None:
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(_canonical_json_bytes(value))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def reconciliation_report(session_maker, run) -> dict[str, Any]:
    with session_scope(session_maker) as session:
        outcome_counts = {
            str(outcome): int(count)
            for outcome, count in session.execute(
                select(
                    tx.ProjectionReconciliationItem.outcome,
                    func.count(tx.ProjectionReconciliationItem.reconciliation_item_id),
                )
                .where(
                    tx.ProjectionReconciliationItem.reconciliation_run_id
                    == run.reconciliation_run_id
                )
                .group_by(tx.ProjectionReconciliationItem.outcome)
                .order_by(tx.ProjectionReconciliationItem.outcome)
            )
        }
    report = {
        "format": "dish-projection-reconciliation-report-v1",
        "status": "pass" if run.status == "complete" else "fail",
        "ok": run.status == "complete",
        "reconciliation_run_id": str(run.reconciliation_run_id),
        "generation_id": str(run.generation_id),
        "projection_epoch_id": str(run.projection_epoch_id),
        "corpus_identity": run.corpus_identity,
        "run_status": run.status,
        "expected_items": int(run.expected_items),
        "processed_items": int(run.processed_items),
        "outcome_counts": outcome_counts,
        "started_at": run.started_at.isoformat(),
        "completed_at": None if run.completed_at is None else run.completed_at.isoformat(),
    }
    report["report_sha256"] = hashlib.sha256(_canonical_json_bytes(report)).hexdigest()
    return report


@dataclass(frozen=True)
class ExternalCorpusItem:
    """One externally fetched item, normalized by the injected fetch adapter."""

    item_identity: str
    entity_kind: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ReconciliationRecord:
    """Pure comparison result passed unchanged to ProjectionService."""

    item_identity: str
    entity_kind: str
    mapping_id: uuid.UUID | None
    outcome: str
    evidence: Mapping[str, Any]


class FetchCorpusCallable(Protocol):
    def __call__(self, corpus_identity: str) -> Sequence[ExternalCorpusItem]:
        """Fetch the complete external corpus with no database transaction open."""


class CompareItemCallable(Protocol):
    def __call__(
        self,
        session: Session,
        generation_id: uuid.UUID,
        item: ExternalCorpusItem,
    ) -> ReconciliationRecord:
        """Compare one already-fetched item without performing external I/O."""


class ReconciliationWorker:
    """Periodically execute governed, corpus-scoped reconciliation runs."""

    def __init__(
        self,
        *,
        session_maker,
        fetch_corpus: FetchCorpusCallable,
        compare_item: CompareItemCallable,
        generation_id: uuid.UUID,
        corpus_identity: str,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        if not corpus_identity.strip():
            raise ValueError("corpus_identity must be non-blank")
        self._session_maker = session_maker
        self._fetch_corpus = fetch_corpus
        self._compare_item = compare_item
        self._generation_id = generation_id
        self._corpus_identity = corpus_identity
        self._clock = clock
        self._stop = False

    def request_shutdown(self) -> None:
        """Stop before the next corpus run; an active run is allowed to finish."""

        self._stop = True

    def run_once(self):
        """Fetch and reconcile one complete corpus, returning the governed run."""

        # External I/O must finish before opening the authoritative transaction.
        corpus = tuple(self._fetch_corpus(self._corpus_identity))
        if self._stop:
            LOGGER.info("shutdown requested before reconciliation transaction")
            return None
        started_at = self._clock()
        with session_scope(self._session_maker) as session:
            service = ProjectionService(session)
            run = service.start_reconciliation(
                generation_id=self._generation_id,
                corpus_identity=self._corpus_identity,
                expected_items=len(corpus),
                started_at=started_at,
            )
            for item in corpus:
                record = self._compare_item(session, self._generation_id, item)
                service.record_reconciliation_item(
                    reconciliation_run_id=run.reconciliation_run_id,
                    item_identity=record.item_identity,
                    entity_kind=record.entity_kind,
                    mapping_id=record.mapping_id,
                    outcome=record.outcome,
                    evidence=record.evidence,
                    recorded_at=self._clock(),
                )
            return service.complete_reconciliation(
                reconciliation_run_id=run.reconciliation_run_id,
                completed_at=self._clock(),
            )


def load_callable(import_path: str):
    module_name, separator, attribute_name = import_path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("callable must use module:attribute syntax")
    value = getattr(importlib.import_module(module_name), attribute_name)
    return value() if isinstance(value, type) else value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile one complete external corpus")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--generation-id", type=uuid.UUID, required=True)
    parser.add_argument("--corpus-identity", required=True)
    parser.add_argument(
        "--fetcher",
        required=True,
        help="module:callable that fetches the full external corpus",
    )
    parser.add_argument(
        "--comparator",
        required=True,
        help="module:callable that compares one fetched item without external I/O",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--output",
        type=Path,
        help="write a mode-0600 machine-checkable report for this exact run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    engine = create_database_engine(DatabaseSettings(url=args.database_url))
    worker = ReconciliationWorker(
        session_maker=session_factory(engine),
        fetch_corpus=load_callable(args.fetcher),
        compare_item=load_callable(args.comparator),
        generation_id=args.generation_id,
        corpus_identity=args.corpus_identity,
    )

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        del frame
        LOGGER.info("shutdown requested", extra={"signal": signum})
        worker.request_shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        run = worker.run_once()
        if run is None:
            report = {
                "format": "dish-projection-reconciliation-report-v1",
                "status": "not_run",
                "ok": False,
                "generation_id": str(args.generation_id),
                "corpus_identity": args.corpus_identity,
                "reason": "shutdown requested before the authoritative transaction",
            }
            report["report_sha256"] = hashlib.sha256(_canonical_json_bytes(report)).hexdigest()
        else:
            report = reconciliation_report(session_factory(engine), run)
        if args.output is not None:
            _write_atomic(args.output, report)
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0 if report["ok"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
