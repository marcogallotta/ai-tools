"""Dark-launch legacy-spool delivery and PostgreSQL shadow execution worker.

This worker is separate from projection_worker and reconciliation_worker. It
never calls Asana. PostgreSQL command execution may create transactional
projection intents tagged with origin ``shadow``. Projection workers reject
those rows unconditionally, independent of epoch effect configuration. The
shared filesystem kill switch stops both new legacy capture and this worker.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Mapping, Protocol

from sqlalchemy import select

from dish_service.shadow_spool import ShadowSpool, ShadowSpoolItem

from . import stage3_models as wf
from . import stage5_models as tx
from .command_port import CommandCall, CommandResult, PostgresCommandPort
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .transition import ShadowService
from .workflow import WorkflowAuthorityService

LOGGER = logging.getLogger("dish.shadow_worker")
_NAMESPACE = uuid.UUID("b40de1df-43e5-445c-9eed-c87f17d2b526")
_IDENTIFIER_ROLES = {
    "submission_id": ("operation_id", "operation"),
    "operation_id": ("operation_id", "operation"),
    "existing_submission_id": ("operation_id", "operation"),
    "prepared_operation_id": ("successor_operation_id", "operation"),
    "successor_operation_id": ("successor_operation_id", "operation"),
    "lease_id": ("lease_id", "lease"),
    "cycle_id": ("cycle_id", "verification_cycle"),
    "verification_cycle_id": ("cycle_id", "verification_cycle"),
    "new_cycle_id": ("cycle_id", "verification_cycle"),
    "prepared_cycle_id": ("prepared_cycle_id", "verification_cycle"),
    "intent_challenge_id": ("challenge_id", "planning_challenge"),
    "challenge_id": ("challenge_id", "planning_challenge"),
    "attempt_id": ("abandonment_id", "abandonment"),
    "abandonment_id": ("abandonment_id", "abandonment"),
    "requirement_id": ("requirement_id", "human_review_requirement"),
    "hold_id": ("hold_id", "evidence_hold"),
    "grant_id": ("grant_id", "authorization_grant"),
}



class ShadowIdentityMappingError(ValueError):
    """A captured source authority identifier has no unique target binding."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _shadow_uuid(envelope: tx.ShadowEnvelope, *, label: str, value: Any) -> uuid.UUID:
    """Namespace source identities so shadow rows cannot collide with live authority."""
    return uuid.uuid5(
        _NAMESPACE,
        ":".join(
            (
                label,
                str(envelope.shadow_baseline_id),
                str(envelope.source_authority_generation or "unknown-source-generation"),
                str(value),
            )
        ),
    )


def _shadow_agent(arguments: Mapping[str, Any], owner_id: str) -> str:
    allowed = {"claude", "gpt", "codex", "marco", "service"}
    candidates = (arguments.get("agent"), owner_id.rsplit(":", 1)[-1])
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower()
        if normalized in allowed:
            return normalized
    return "service"


def _ensure_shadow_run(
    session,
    *,
    envelope: tx.ShadowEnvelope,
    arguments: Mapping[str, Any],
    owner_id: str,
    source_run_identity: Any,
    target_run_id: uuid.UUID,
    generation_id: uuid.UUID,
) -> wf.ServiceRun:
    agent = _shadow_agent(arguments, owner_id)
    capability_digest = hashlib.sha256(
        "\0".join(
            (
                "dish-shadow-run-v1",
                str(envelope.shadow_baseline_id),
                str(envelope.source_authority_generation or ""),
                owner_id,
                str(source_run_identity),
                agent,
            )
        ).encode("utf-8")
    ).digest()
    existing = session.get(wf.ServiceRun, target_run_id)
    if existing is not None:
        if (
            existing.generation_id != generation_id
            or existing.owner_id != owner_id
            or existing.agent != agent
            or existing.capability_digest != capability_digest
            or existing.status != "active"
        ):
            raise ValueError("shadow run identity conflicts with existing target authority")
        return existing
    return WorkflowAuthorityService(session).register_run(
        run_id=target_run_id,
        generation_id=generation_id,
        owner_id=owner_id,
        agent=agent,
        capability_digest=capability_digest,
        registered_at=envelope.captured_at,
    )


def _collect_identifiers(value: Any) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                role = _IDENTIFIER_ROLES.get(str(key))
                if (
                    role is not None
                    and child is not None
                    and child != ""
                    and not isinstance(child, (Mapping, list, tuple))
                ):
                    found.setdefault(role[0], set()).add(str(child))
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return found


def _identifier_bindings(session, envelope: tx.ShadowEnvelope) -> dict[str, dict[str, str]]:
    query = (
        select(tx.ShadowEnvelope, tx.ShadowComparison)
        .join(tx.ShadowComparison, tx.ShadowComparison.envelope_id == tx.ShadowEnvelope.envelope_id)
        .where(tx.ShadowEnvelope.shadow_baseline_id == envelope.shadow_baseline_id)
    )
    if envelope.rollout_sequence is not None:
        query = query.where(
            tx.ShadowEnvelope.rollout_sequence.is_not(None),
            tx.ShadowEnvelope.rollout_sequence < envelope.rollout_sequence,
        ).order_by(tx.ShadowEnvelope.rollout_sequence)
    else:
        query = query.where(tx.ShadowEnvelope.captured_at < envelope.captured_at).order_by(
            tx.ShadowEnvelope.captured_at, tx.ShadowEnvelope.envelope_id
        )

    bindings: dict[str, dict[str, str]] = {}
    conflicts: set[tuple[str, str]] = set()
    for prior, comparison in session.execute(query):
        source = _collect_identifiers(prior.source_outcome)
        target = _collect_identifiers(comparison.target_result)
        for role in source.keys() & target.keys():
            source_values, target_values = source[role], target[role]
            if len(source_values) != 1 or len(target_values) != 1:
                continue
            source_value, target_value = next(iter(source_values)), next(iter(target_values))
            family = next(family for candidate, family in _IDENTIFIER_ROLES.values() if candidate == role)
            family_bindings = bindings.setdefault(family, {})
            existing = family_bindings.get(source_value)
            if existing is not None and existing != target_value:
                conflicts.add((family, source_value))
                family_bindings.pop(source_value, None)
            elif (family, source_value) not in conflicts:
                family_bindings[source_value] = target_value
    return bindings


def _translate_workflow_identifiers(
    session,
    envelope: tx.ShadowEnvelope,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    translated = dict(arguments)
    needed = {
        key: _IDENTIFIER_ROLES[key][1]
        for key, value in arguments.items()
        if key in _IDENTIFIER_ROLES and value is not None and value != ""
    }
    if not needed:
        return translated
    bindings = _identifier_bindings(session, envelope)
    for key, family in needed.items():
        source_value = str(arguments[key])
        target_value = bindings.get(family, {}).get(source_value)
        if target_value is None:
            raise ShadowIdentityMappingError(
                f"no unique target {family} binding for captured field {key}"
            )
        translated[key] = target_value
    return translated


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
        owner_id = str(principal.get("owner_id") or "legacy-shadow")
        source_run_identity = principal.get("run_id") or f"request:{envelope.source_request_identity}"
        target_run_id = _shadow_uuid(
            envelope, label="run", value=f"{owner_id}:{source_run_identity}"
        )
        port = PostgresCommandPort(
            session, cursor_secret=self.cursor_secret, projection_origin="shadow"
        )
        generation = port.reads.active_generation()
        _ensure_shadow_run(
            session,
            envelope=envelope,
            arguments=arguments,
            owner_id=owner_id,
            source_run_identity=source_run_identity,
            target_run_id=target_run_id,
            generation_id=generation.generation_id,
        )
        result = port.execute(
            CommandCall(
                command_name=envelope.command_name,
                arguments=_translate_workflow_identifiers(session, envelope, arguments),
                owner_id=owner_id,
                principal_class=str(principal.get("principal_class") or "agent"),
                run_id=target_run_id,
                request_id=_shadow_uuid(
                    envelope, label="request", value=envelope.source_request_identity
                ),
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
        kill_switch_path: Path,
        claim_ttl: timedelta = timedelta(minutes=2),
        delivered_retention: timedelta = timedelta(days=7),
        idle_seconds: float = 1.0,
        clock=_utcnow,
    ) -> None:
        self.spool = spool
        self.session_maker = session_maker
        self.baseline_id = baseline_id
        self.evaluator = evaluator
        self.worker_id = worker_id
        self.comparator_release = comparator_release
        self.kill_switch_path = Path(kill_switch_path)
        if delivered_retention.total_seconds() < 0:
            raise ValueError("delivered_retention must not be negative")
        self.claim_ttl = claim_ttl
        self.delivered_retention = delivered_retention
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._stop = False

    def request_shutdown(self) -> None:
        self._stop = True

    def _kill_switch_engaged(self) -> bool:
        return self.kill_switch_path.exists()

    def run_forever(self) -> None:
        while not self._stop:
            if self._kill_switch_engaged():
                LOGGER.warning(
                    "dark-launch kill switch engaged; shadow worker exiting",
                    extra={"kill_switch": str(self.kill_switch_path)},
                )
                return
            if not self.run_once():
                time.sleep(self.idle_seconds)

    def run_once(self) -> bool:
        if self._kill_switch_engaged():
            return False
        try:
            self.spool.compact_delivered(
                now=self.clock(), older_than=self.delivered_retention, limit=1000
            )
        except BaseException:
            LOGGER.exception("shadow spool delivered-payload compaction failed")
        pending = self.spool.pending(limit=1)
        if pending:
            item = pending[0]
            try:
                self._deliver(item)
            except BaseException as exc:
                LOGGER.exception("shadow spool delivery failed")
                self.spool.mark_delivery_failed(item.registration_id, error=str(exc))
                return True
            delivered_at = self.clock()
            self.spool.mark_delivered(item.registration_id, delivered_at=delivered_at)
            try:
                self.spool.compact_delivered(
                    now=delivered_at, older_than=self.delivered_retention, limit=1000
                )
            except BaseException:
                LOGGER.exception("shadow spool delivered-payload compaction failed")
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
    parser.add_argument("--kill-switch", required=True, type=Path)
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--delivered-retention-seconds", type=int, default=7 * 24 * 60 * 60)
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
        kill_switch_path=args.kill_switch,
        delivered_retention=timedelta(seconds=args.delivered_retention_seconds),
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
