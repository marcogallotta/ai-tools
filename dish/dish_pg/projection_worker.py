"""Projection worker: drains pending projection_outbox_events.

This process owns transaction boundaries and all external I/O. It never
reimplements outbox claim/apply/adjudicate logic — that authority lives
entirely in :class:`dish_pg.transition.ProjectionService`, which performs no
network I/O or commits itself (see that module's docstring). Each claimed
event is processed as three separately committed steps so a crash between
any two of them leaves recoverable, non-lost state:

1. claim (own transaction) — mark the event claimed;
2. begin_attempt (own transaction) — durably record intent *before* the
   external call, per the checkpoint in database-backend-imp.md Section 2;
3. for a first dispatch, the external mutation happens with no open
   transaction and the adapter independently rereads it; for a reclaimed
   active attempt, only the read-only recovery observation is permitted;
4. record_observation_and_adjudicate (own transaction) settles the event under
   the exact still-current claim token and revision.

Callers supply an :class:`ExternalAdapter` that performs the actual external
system call and operation-specific reread; this module has no knowledge of any
specific external API.
"""
from __future__ import annotations

import argparse
import importlib
import logging
import signal
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import FrameType
from typing import Any, Mapping, Protocol, Sequence

from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .transition import ProjectionClaim, ProjectionService, TransitionAuthorityError

LOGGER = logging.getLogger("dish.projection_worker")


@dataclass(frozen=True)
class ExternalAttempt:
    """What the adapter intends to do, decided before the external call."""

    request_identity: str
    request_payload: Mapping[str, Any]
    intended_external_id: str | None = None


@dataclass(frozen=True)
class ExternalObservation:
    """What the adapter actually observed, after the external call."""

    observed_applied: bool | None
    observed_identity: str | None
    reread_complete: bool
    evidence: Mapping[str, Any]
    observation_kind: str = "reread"
    decided_by: str = "automatic"
    decision_reason: str = "external observation"
    # Only meaningful for event_type == "create_task": the exact set of
    # external matches found by a marker search, if one was performed this
    # round. None means "no marker search performed yet".
    create_matches: Sequence[str] | None = None


class ExternalAdapter(Protocol):
    def prepare(self, claim: ProjectionClaim) -> ExternalAttempt:
        """Decide what to send, without making any external call yet."""

    def attempt_and_observe(
        self, claim: ProjectionClaim, attempt: ExternalAttempt
    ) -> ExternalObservation:
        """Perform the first external mutation, then independently observe it."""

    def observe_recovery(
        self, claim: ProjectionClaim, attempt: ExternalAttempt
    ) -> ExternalObservation:
        """Observe a possibly dispatched mutation without sending it again."""


class ProjectionWorker:
    def __init__(
        self,
        *,
        session_maker,
        adapter: ExternalAdapter,
        worker_id: str,
        claim_ttl: timedelta = timedelta(minutes=2),
        idle_seconds: float = 1.0,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must be non-blank")
        self._session_maker = session_maker
        self._adapter = adapter
        self._worker_id = worker_id
        self._claim_ttl = claim_ttl
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._stop = False

    def request_shutdown(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        while not self._stop:
            if not self.run_once():
                if self._stop:
                    return
                time.sleep(self._idle_seconds)

    def run_once(self) -> bool:
        """Process at most one event. Returns False if nothing was pending."""

        claim = self._claim()
        if claim is None:
            return False
        try:
            if claim.recovery_required:
                attempt_id, request = self._begin_recovery_attempt(claim)
                observation = self._adapter.observe_recovery(claim, request)
            else:
                attempt_id, request = self._begin_attempt(claim)
                observation = self._adapter.attempt_and_observe(claim, request)
            self._settle(claim, attempt_id, observation)
        except TransitionAuthorityError:
            LOGGER.exception(
                "projection claim lost mid-processing; another worker or expiry reclaimed it",
                extra={"event_id": str(claim.event_id)},
            )
        return True

    def _claim(self) -> ProjectionClaim | None:
        with session_scope(self._session_maker) as session:
            service = ProjectionService(session)
            return service.claim_next(
                worker_id=self._worker_id, now=self._clock(), ttl=self._claim_ttl
            )

    def _begin_attempt(self, claim: ProjectionClaim) -> tuple[uuid.UUID, ExternalAttempt]:
        request = self._adapter.prepare(claim)
        with session_scope(self._session_maker) as session:
            service = ProjectionService(session)
            attempt = service.begin_attempt(
                event_id=claim.event_id,
                claim_token=claim.claim_token,
                claim_revision=claim.claim_revision,
                worker_id=self._worker_id,
                request_identity=request.request_identity,
                request_payload=request.request_payload,
                intended_external_id=request.intended_external_id,
                started_at=self._clock(),
            )
            durable_request = replace(request, request_identity=attempt.dispatch_identity)
            return attempt.attempt_id, durable_request

    def _begin_recovery_attempt(
        self, claim: ProjectionClaim
    ) -> tuple[uuid.UUID, ExternalAttempt]:
        prior = claim.recovery_attempt
        if prior is None:
            raise TransitionAuthorityError("projection claim does not require recovery")
        with session_scope(self._session_maker) as session:
            service = ProjectionService(session)
            attempt = service.begin_recovery_attempt(
                event_id=claim.event_id,
                claim_token=claim.claim_token,
                claim_revision=claim.claim_revision,
                worker_id=self._worker_id,
                prior_attempt_id=prior.attempt_id,
                started_at=self._clock(),
            )
            return attempt.attempt_id, ExternalAttempt(
                request_identity=prior.external_dispatch_identity,
                request_payload=dict(prior.request_payload),
                intended_external_id=prior.intended_external_id,
            )

    def _settle(
        self,
        claim: ProjectionClaim,
        attempt_id: uuid.UUID,
        observation: ExternalObservation,
    ) -> None:
        now = self._clock()
        with session_scope(self._session_maker) as session:
            service = ProjectionService(session)
            if claim.event_type == "create_task" and observation.create_matches is not None:
                service.resolve_create_correlation(
                    event_id=claim.event_id,
                    attempt_id=attempt_id,
                    external_matches=observation.create_matches,
                    observed_at=now,
                    evidence=observation.evidence,
                    claim_token=claim.claim_token,
                    claim_revision=claim.claim_revision,
                    worker_id=self._worker_id,
                )
            service.record_observation_and_adjudicate(
                attempt_id=attempt_id,
                observation_kind=observation.observation_kind,
                observed_applied=observation.observed_applied,
                observed_identity=observation.observed_identity,
                reread_complete=observation.reread_complete,
                evidence=observation.evidence,
                decided_by=observation.decided_by,
                decision_reason=observation.decision_reason,
                observed_at=now,
                claim_token=claim.claim_token,
                claim_revision=claim.claim_revision,
                worker_id=self._worker_id,
            )


def load_adapter(import_path: str) -> ExternalAdapter:
    module_name, separator, attribute_name = import_path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("adapter must use module:attribute syntax")
    value = getattr(importlib.import_module(module_name), attribute_name)
    return value() if isinstance(value, type) else value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drain projection_outbox_events")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--adapter", required=True, help="module:ClassOrInstance implementing ExternalAdapter"
    )
    parser.add_argument("--claim-ttl-seconds", type=int, default=120)
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--once",
        action="store_true",
        help="process at most one claim and exit; useful for supervised one-shot execution",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    engine = create_database_engine(DatabaseSettings(url=args.database_url))
    worker = ProjectionWorker(
        session_maker=session_factory(engine),
        adapter=load_adapter(args.adapter),
        worker_id=args.worker_id,
        claim_ttl=timedelta(seconds=args.claim_ttl_seconds),
        idle_seconds=args.idle_seconds,
    )

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        del frame
        LOGGER.info("shutdown requested", extra={"signal": signum})
        worker.request_shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        if args.once:
            worker.run_once()
        else:
            worker.run_forever()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
