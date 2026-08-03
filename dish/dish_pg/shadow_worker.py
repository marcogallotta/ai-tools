"""Dark-launch legacy-spool delivery and PostgreSQL shadow execution worker.

This worker is separate from projection_worker and reconciliation_worker. It
never calls Asana. PostgreSQL command execution may create transactional
projection intents tagged with origin ``shadow``. Projection workers reject
those rows unconditionally, independent of epoch effect configuration.
"""
from __future__ import annotations

import argparse
import logging
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Mapping, Protocol

from dish_service.shadow_spool import ShadowSpool, ShadowSpoolItem

from . import stage5_models as tx
from .command_port import CommandCall, CommandResult, PostgresCommandPort
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .transition import ShadowService

LOGGER = logging.getLogger("dish.shadow_worker")
_NAMESPACE = uuid.UUID("b40de1df-43e5-445c-9eed-c87f17d2b526")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value: Any, *, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return uuid.uuid5(_NAMESPACE, f"{label}:{value}")


def _result_payload(result: CommandResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "command": result.command,
        "code": result.code,
        "http_status": result.http_status,
        "data": dict(result.data),
        "retryable": result.retryable,
    }


def semantic_normalizer(value: Mapping[str, Any]) -> Any:
    """Normalize only transport/replay metadata, never workflow semantics."""
    def clean(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): clean(child)
                for key, child in item.items()
                if key not in {"request_replayed", "captured_at", "service_cleanup_warning"}
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item
    return clean(value)


class ShadowEvaluator(Protocol):
    def evaluate(self, session, envelope: tx.ShadowEnvelope) -> Mapping[str, Any]: ...


class CommandPortShadowEvaluator:
    def __init__(self, *, cursor_secret: bytes) -> None:
        self.cursor_secret = cursor_secret

    def evaluate(self, session, envelope: tx.ShadowEnvelope) -> Mapping[str, Any]:
        canonical = dict(envelope.canonical_input)
        arguments = canonical.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ValueError("shadow envelope arguments are missing")
        principal = dict(envelope.principal or {})
        port = PostgresCommandPort(
            session, cursor_secret=self.cursor_secret, projection_origin="shadow"
        )
        result = port.execute(
            CommandCall(
                command_name=envelope.command_name,
                arguments=dict(arguments),
                owner_id=str(principal.get("owner_id") or "legacy-shadow"),
                principal_class=str(principal.get("principal_class") or "agent"),
                run_id=_uuid(principal.get("run_id"), label="run"),
                request_id=_uuid(envelope.source_request_identity, label="request"),
                now=envelope.captured_at,
            )
        )
        return _result_payload(result)


class ShadowWorker:
    def __init__(
        self,
        *,
        spool: ShadowSpool,
        session_maker,
        baseline_id: uuid.UUID,
        evaluator: ShadowEvaluator,
        worker_id: str,
        comparator_release: str,
        claim_ttl: timedelta = timedelta(minutes=2),
        idle_seconds: float = 1.0,
        clock=_utcnow,
    ) -> None:
        self.spool = spool
        self.session_maker = session_maker
        self.baseline_id = baseline_id
        self.evaluator = evaluator
        self.worker_id = worker_id
        self.comparator_release = comparator_release
        self.claim_ttl = claim_ttl
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._stop = False

    def request_shutdown(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        while not self._stop:
            if not self.run_once():
                time.sleep(self.idle_seconds)

    def run_once(self) -> bool:
        pending = self.spool.pending(limit=1)
        if pending:
            item = pending[0]
            try:
                self._deliver(item)
            except BaseException as exc:
                LOGGER.exception("shadow spool delivery failed")
                self.spool.mark_delivery_failed(item.registration_id, error=str(exc))
                return True
            self.spool.mark_delivered(item.registration_id, delivered_at=self.clock())
            if item.state == "gap":
                return True
        return self._evaluate_one() or bool(pending)

    def _deliver(self, item: ShadowSpoolItem) -> None:
        with session_scope(self.session_maker) as session:
            service = ShadowService(session)
            if item.state == "gap":
                service.record_gap(
                    baseline_id=self.baseline_id,
                    gap_identity=f"capture:{item.source_request_identity}",
                    gap_kind="missing_envelope",
                    details=dict(item.gap or {}),
                    created_at=item.completed_at or item.created_at,
                )
                return
            if item.source_outcome is None or item.source_post_state is None:
                raise ValueError("complete spool item lacks outcome or post-state")
            service.capture_envelope(
                shadow_baseline_id=self.baseline_id,
                command_name=item.command_name,
                source_request_identity=item.source_request_identity,
                canonical_input=item.canonical_input,
                source_outcome=item.source_outcome,
                source_post_state=item.source_post_state,
                captured_at=item.completed_at or item.created_at,
                rollout_sequence=item.rollout_sequence,
                source_authority_generation=item.source_authority_generation,
                source_execution_identity=item.source_request_identity,
                principal=item.principal,
                source_pre_state=item.source_pre_state,
                pinned_inputs=item.pinned_inputs,
                source_effects=item.source_effects,
                capture_qualification=item.treatment,
            )

    def _evaluate_one(self) -> bool:
        token = uuid.uuid4()
        with session_scope(self.session_maker) as session:
            delivery = ShadowService(session).claim_delivery(
                worker_id=self.worker_id,
                claim_token=token,
                now=self.clock(),
                ttl=self.claim_ttl,
                shadow_baseline_id=self.baseline_id,
            )
            if delivery is None:
                return False
            delivery_id = delivery.delivery_id
            envelope_id = delivery.envelope_id
        try:
            with session_scope(self.session_maker) as session:
                envelope = session.get(tx.ShadowEnvelope, envelope_id)
                rollout_mode = dict(envelope.pinned_inputs or {}).get("rollout_mode")
                if rollout_mode != "execute" or envelope.capture_qualification != "execute":
                    reason = (
                        f"dark-launch rollout mode is {rollout_mode or 'capture'}"
                        if rollout_mode != "execute"
                        else f"dark-launch treatment is {envelope.capture_qualification}"
                    )
                    ShadowService(session).skip_delivery(
                        delivery_id=delivery_id,
                        claim_token=token,
                        reason=reason,
                        comparator_release=self.comparator_release,
                        completed_at=self.clock(),
                    )
                else:
                    target = self.evaluator.evaluate(session, envelope)
                    ShadowService(session).compare_delivery(
                        delivery_id=delivery_id,
                        claim_token=token,
                        target_result=target,
                        comparator_release=self.comparator_release,
                        compared_at=self.clock(),
                        semantic_normalizer=semantic_normalizer,
                    )
        except BaseException as exc:
            with session_scope(self.session_maker) as session:
                ShadowService(session).fail_delivery(
                    delivery_id=delivery_id,
                    claim_token=token,
                    error=str(exc),
                    failed_at=self.clock(),
                )
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drain legacy dark-launch captures")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--spool-path", required=True, type=Path)
    parser.add_argument("--baseline-id", required=True, type=uuid.UUID)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--cursor-secret-file", required=True, type=Path)
    parser.add_argument("--comparator-release", required=True)
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    secret = args.cursor_secret_file.read_bytes().strip()
    if len(secret) < 32:
        raise SystemExit("cursor secret must contain at least 32 bytes")
    engine = create_database_engine(DatabaseSettings(url=args.database_url))
    worker = ShadowWorker(
        spool=ShadowSpool(args.spool_path),
        session_maker=session_factory(engine),
        baseline_id=args.baseline_id,
        evaluator=CommandPortShadowEvaluator(cursor_secret=secret),
        worker_id=args.worker_id,
        comparator_release=args.comparator_release,
        idle_seconds=args.idle_seconds,
    )
    def stop(signum: int, frame: FrameType | None) -> None:
        del frame
        LOGGER.info("shutdown requested", extra={"signal": signum})
        worker.request_shutdown()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        worker.run_forever()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
