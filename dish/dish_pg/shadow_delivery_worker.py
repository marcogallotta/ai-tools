"""Durable spool delivery and comparison orchestration for dark launch."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from dish_service.path_safety import require_distinct_paths
from dish_service.shadow_spool import ShadowSpool, ShadowSpoolItem
from dish_shadow.policy import treatment_for
from dish_tool.constants import RECOVERY_QUARANTINE_SECONDS

from . import models
from . import stage5_models as tx
from .database import session_scope
from .shadow_comparison import semantic_normalizer
from .transition import ShadowService, TransitionAuthorityError

LOGGER = logging.getLogger("dish.shadow_worker")


class PermanentShadowDeliveryError(ValueError):
    """A legacy spool item cannot be delivered by retrying unchanged input."""


def _is_permanent_spool_delivery_error(exc: BaseException) -> bool:
    """Classify deterministic delivery failures that cannot heal on retry."""
    return isinstance(
        exc,
        (TransitionAuthorityError, PermanentShadowDeliveryError, IntegrityError),
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ShadowEvaluator(Protocol):
    def evaluate(self, session, envelope: tx.ShadowEnvelope) -> Mapping[str, Any]: ...


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
        reservation_ttl: timedelta = timedelta(seconds=RECOVERY_QUARANTINE_SECONDS),
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
        require_distinct_paths(
            {
                "dark-launch spool": self.spool.path,
                "dark-launch kill switch": self.kill_switch_path,
            }
        )
        if reservation_ttl.total_seconds() < RECOVERY_QUARANTINE_SECONDS:
            raise ValueError(
                f"reservation_ttl must be at least {RECOVERY_QUARANTINE_SECONDS} seconds"
            )
        if delivered_retention.total_seconds() < 0:
            raise ValueError("delivered_retention must not be negative")
        self.claim_ttl = claim_ttl
        self.reservation_ttl = reservation_ttl
        self.delivered_retention = delivered_retention
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._stop = False

    def request_shutdown(self) -> None:
        self._stop = True

    def _kill_switch_engaged(self) -> bool:
        return self.kill_switch_path.exists()

    def _baseline_is_current(self, *, at: datetime) -> bool:
        with session_scope(self.session_maker) as session:
            active = session.scalar(
                select(models.AuthorityGeneration).where(
                    models.AuthorityGeneration.status == "active"
                )
            )
            if active is None:
                return False
            ShadowService(session).disqualify_stale_baselines(
                active_generation_id=active.generation_id,
                reason="active target authority generation changed during dark launch",
                at=at,
            )
            baseline = session.get(tx.ShadowBaseline, self.baseline_id)
            return bool(
                baseline is not None
                and baseline.status == "open"
                and baseline.generation_id == active.generation_id
            )

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
        now = self.clock()
        if not self._baseline_is_current(at=now):
            LOGGER.error(
                "shadow baseline is stale or unavailable; worker is not draining"
            )
            return False
        try:
            self.spool.recover_stale_reservations(
                now=now, older_than=self.reservation_ttl
            )
        except BaseException:
            LOGGER.exception("shadow spool stale-reservation recovery failed")
            return False
        try:
            self.spool.compact_delivered(
                now=now, older_than=self.delivered_retention, limit=1000
            )
        except BaseException:
            LOGGER.exception("shadow spool delivered-payload compaction failed")
        pending = self.spool.pending(limit=1)
        if pending:
            item = pending[0]
            try:
                self._deliver(item)
            except BaseException as exc:
                if item.state == "complete" and _is_permanent_spool_delivery_error(exc):
                    LOGGER.warning(
                        "permanent shadow spool delivery failure; recording comparison gap",
                        exc_info=True,
                    )
                    try:
                        self._record_permanent_spool_delivery_gap(item, exc)
                        delivered_at = self.clock()
                        self.spool.mark_delivered(
                            item.registration_id, delivered_at=delivered_at
                        )
                    except BaseException as terminal_exc:
                        LOGGER.exception(
                            "permanent shadow spool delivery terminalization failed"
                        )
                        failure = (
                            f"{type(exc).__name__}: {exc}; terminalization failed: "
                            f"{type(terminal_exc).__name__}: {terminal_exc}"
                        )
                        try:
                            self.spool.mark_delivery_failed(
                                item.registration_id, error=failure
                            )
                        except BaseException:
                            LOGGER.exception(
                                "shadow spool delivery-failure recording failed"
                            )
                        return False
                    try:
                        self.spool.compact_delivered(
                            now=delivered_at,
                            older_than=self.delivered_retention,
                            limit=1000,
                        )
                    except BaseException:
                        LOGGER.exception(
                            "shadow spool delivered-payload compaction failed"
                        )
                    return True
                LOGGER.exception("shadow spool delivery failed")
                try:
                    self.spool.mark_delivery_failed(
                        item.registration_id, error=str(exc)
                    )
                except BaseException:
                    LOGGER.exception("shadow spool delivery-failure recording failed")
                return False
            delivered_at = self.clock()
            self.spool.mark_delivered(item.registration_id, delivered_at=delivered_at)
            try:
                self.spool.compact_delivered(
                    now=delivered_at,
                    older_than=self.delivered_retention,
                    limit=1000,
                )
            except BaseException:
                LOGGER.exception("shadow spool delivered-payload compaction failed")
            if item.state == "gap":
                return True
        return self._evaluate_one() or bool(pending)

    def _record_permanent_spool_delivery_gap(
        self,
        item: ShadowSpoolItem,
        exc: BaseException,
    ) -> None:
        with session_scope(self.session_maker) as session:
            baseline = session.get(tx.ShadowBaseline, self.baseline_id)
            details = {
                "classification": "permanent",
                "failure_stage": "spool_delivery",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "source_request_identity": item.source_request_identity,
                "source_authority_generation": item.source_authority_generation,
                "rollout_sequence": item.rollout_sequence,
            }
            if baseline is not None:
                details["baseline_source_generation"] = (
                    baseline.source_generation_identity
                )
            ShadowService(session).record_gap(
                baseline_id=self.baseline_id,
                gap_identity=f"spool_delivery:{item.source_request_identity}",
                gap_kind="uncomparable",
                details=details,
                created_at=item.completed_at or item.created_at,
            )

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
                raise PermanentShadowDeliveryError(
                    "complete spool item lacks outcome or post-state"
                )
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
            claim_revision = delivery.delivery_revision
        try:
            with session_scope(self.session_maker) as session:
                envelope = session.get(tx.ShadowEnvelope, envelope_id)
                if envelope is None:
                    raise ValueError("claimed shadow delivery envelope does not exist")
                rollout_mode = dict(envelope.pinned_inputs or {}).get("rollout_mode")
                treatment = treatment_for(envelope.command_name)
                if envelope.capture_qualification != treatment.treatment:
                    raise ValueError(
                        "captured dark-launch treatment contradicts authoritative command "
                        f"treatment: {envelope.command_name} "
                        f"captured={envelope.capture_qualification} "
                        f"authoritative={treatment.treatment}"
                    )
                if rollout_mode != "execute" or not treatment.comparison_eligible:
                    reason = (
                        f"dark-launch rollout mode is {rollout_mode or 'capture'}"
                        if rollout_mode != "execute"
                        else f"dark-launch treatment is {treatment.treatment}"
                    )
                    ShadowService(session).skip_delivery(
                        delivery_id=delivery_id,
                        claim_token=token,
                        claim_revision=claim_revision,
                        worker_id=self.worker_id,
                        reason=reason,
                        comparator_release=self.comparator_release,
                        completed_at=self.clock(),
                    )
                else:
                    target = self.evaluator.evaluate(session, envelope)
                    ShadowService(session).compare_delivery(
                        delivery_id=delivery_id,
                        claim_token=token,
                        claim_revision=claim_revision,
                        worker_id=self.worker_id,
                        target_result=target,
                        comparator_release=self.comparator_release,
                        compared_at=self.clock(),
                        semantic_normalizer=semantic_normalizer,
                    )
        except BaseException as exc:  # noqa: BLE001 - settle every evaluation failure
            try:
                with session_scope(self.session_maker) as session:
                    ShadowService(session).fail_delivery(
                        delivery_id=delivery_id,
                        claim_token=token,
                        claim_revision=claim_revision,
                        worker_id=self.worker_id,
                        error=str(exc),
                        failed_at=self.clock(),
                    )
            except TransitionAuthorityError:
                LOGGER.info(
                    "shadow evaluation lost delivery authority; current state is preserved",
                    extra={"delivery_id": str(delivery_id)},
                )
        return True
