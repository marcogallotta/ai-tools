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
import importlib
import logging
import signal
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import FrameType
from typing import Any, Mapping, Protocol, Sequence

from sqlalchemy.orm import Session

from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .transition import ProjectionService

LOGGER = logging.getLogger("dish.reconciliation_worker")


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
        worker.run_once()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
