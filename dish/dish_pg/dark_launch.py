"""Dark-launch operator controls and bounded status reporting."""
from __future__ import annotations

import argparse
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from dish_service.path_safety import clear_kill_switch, engage_kill_switch, inspect_kill_switch
from dish_service.shadow_spool import ShadowSpool

from . import models
from . import stage5_models as tx
from .dark_launch_readiness import observe_worker_unit, redact_reason
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .transition import ProjectionService, ShadowService

_HEALTH_ORDER = {"healthy": 0, "warning": 1, "unavailable": 2, "critical": 3}


@dataclass(frozen=True)
class StatusThresholds:
    warning_backlog: int | None = None
    critical_backlog: int | None = None
    warning_lag_seconds: float | None = None
    critical_lag_seconds: float | None = None
    warning_capacity_percent: float | None = None
    critical_capacity_percent: float | None = None
    warning_mismatches: int | None = None
    critical_mismatches: int | None = None
    warning_gaps: int | None = None
    critical_gaps: int | None = None

    def validate(self) -> None:
        for name in (
            "backlog",
            "lag_seconds",
            "capacity_percent",
            "mismatches",
            "gaps",
        ):
            warning = getattr(self, f"warning_{name}")
            critical = getattr(self, f"critical_{name}")
            if warning is None and critical is None:
                continue
            if warning is None or critical is None:
                raise ValueError(f"both warning and critical {name} thresholds are required")
            if warning < 0 or critical < 0:
                raise ValueError(f"{name} thresholds must not be negative")
            if critical < warning:
                raise ValueError(f"critical {name} threshold must be at least warning")
        if (
            self.warning_capacity_percent is not None
            and self.critical_capacity_percent is not None
            and self.critical_capacity_percent > 100
        ):
            raise ValueError("capacity thresholds must not exceed 100 percent")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _threshold_health(
    value: float | int | None,
    *,
    warning: float | int | None,
    critical: float | int | None,
) -> dict[str, Any]:
    if value is None or warning is None or critical is None:
        return {
            "state": "unavailable",
            "value": value,
            "warning_threshold": warning,
            "critical_threshold": critical,
        }
    state = "critical" if value >= critical else ("warning" if value >= warning else "healthy")
    return {
        "state": state,
        "value": value,
        "warning_threshold": warning,
        "critical_threshold": critical,
    }


def _overall_health(items: list[dict[str, Any]]) -> str:
    if not items:
        return "unavailable"
    return max((str(item["state"]) for item in items), key=lambda state: _HEALTH_ORDER[state])


def _worker_health(worker: dict[str, Any] | None) -> dict[str, Any]:
    if worker is None:
        return {"state": "unavailable", "reason": "worker unit state was not requested"}
    if worker.get("state") == "unavailable":
        return {
            "state": "unavailable",
            "reason": str(worker.get("reason") or "worker unit state is unavailable"),
        }
    if worker.get("load_state") != "loaded":
        state = "unavailable"
    elif worker.get("active_state") == "failed" or worker.get("result") not in {"", "success"}:
        state = "critical"
    elif worker.get("active_state") == "active" and worker.get("sub_state") == "running":
        state = "healthy"
    else:
        state = "warning"
    return {
        "state": state,
        "load_state": worker.get("load_state"),
        "active_state": worker.get("active_state"),
        "sub_state": worker.get("sub_state"),
        "unit_file_state": worker.get("unit_file_state"),
        "result": worker.get("result"),
    }


@contextmanager
def _read_only_session(session_maker):
    session = session_maker()
    transaction = session.begin()
    try:
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        yield session
    finally:
        if transaction.is_active:
            transaction.rollback()
        session.close()


def status(
    *,
    session_maker,
    spool: ShadowSpool,
    baseline_id: uuid.UUID | None,
    kill_switch_path: Path | None = None,
    worker_unit: dict[str, Any] | None = None,
    thresholds: StatusThresholds | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observation = (observed_at or _now()).astimezone(timezone.utc)
    threshold_values = thresholds or StatusThresholds()
    threshold_values.validate()
    report: dict[str, Any] = {
        "format": "dish-postgresql-dark-launch-status-v2",
        "observed_at": observation.isoformat(),
        "read_only": True,
    }

    backlog: int | None = None
    oldest_age: float | None = None
    capacity_percent: float | None = None
    try:
        spool_report = dict(spool.status())
        oldest_pending = _parse_timestamp(spool_report.get("oldest_pending_at"))
        oldest_age = (
            None
            if oldest_pending is None
            else max(0.0, (observation - oldest_pending).total_seconds())
        )
        counts = dict(spool_report["counts"])
        backlog = (
            int(counts.get("reserved", 0))
            + int(counts.get("complete", 0))
            + int(counts.get("gap", 0))
        )
        capacity = dict(spool_report["capacity"])
        record_percent = (
            100.0 * int(capacity["total_records"]) / int(capacity["max_records"])
        )
        byte_percent = (
            100.0 * int(capacity["logical_bytes"]) / int(capacity["max_bytes"])
        )
        free_floor_percent = (
            100.0
            if int(capacity["free_bytes"]) < int(capacity["min_free_bytes"])
            else 0.0
        )
        capacity_percent = round(
            min(100.0, max(record_percent, byte_percent, free_floor_percent)), 3
        )
        report["spool"] = {
            "state": "available",
            **spool_report,
            "backlog": backlog,
            "oldest_pending_age_seconds": (
                None if oldest_age is None else round(oldest_age, 3)
            ),
            "capacity_percent": capacity_percent,
        }
    except Exception as exc:
        report["spool"] = {
            "state": "unavailable",
            "reason": redact_reason(exc),
            "counts": None,
            "backlog": None,
            "oldest_pending_at": None,
            "oldest_pending_age_seconds": None,
            "capacity": None,
            "capacity_percent": None,
        }

    mismatch_count: int | None = None
    gap_count: int | None = None
    report["active_generation_id"] = None
    report["baseline"] = None
    report["projection_epoch"] = None
    try:
        with _read_only_session(session_maker) as session:
            generation = session.scalar(
                select(models.AuthorityGeneration).where(
                    models.AuthorityGeneration.status == "active"
                )
            )
            report["active_generation_id"] = (
                None if generation is None else str(generation.generation_id)
            )
            if baseline_id is None:
                baseline = session.scalar(
                    select(tx.ShadowBaseline)
                    .where(tx.ShadowBaseline.status == "open")
                    .order_by(tx.ShadowBaseline.created_at.desc())
                )
            else:
                baseline = session.get(tx.ShadowBaseline, baseline_id)
            if baseline is not None:
                delivery_counts = {
                    state: int(count)
                    for state, count in session.execute(
                        select(tx.ShadowDelivery.state, func.count())
                        .join(
                            tx.ShadowEnvelope,
                            tx.ShadowEnvelope.envelope_id == tx.ShadowDelivery.envelope_id,
                        )
                        .where(
                            tx.ShadowEnvelope.shadow_baseline_id
                            == baseline.shadow_baseline_id
                        )
                        .group_by(tx.ShadowDelivery.state)
                    )
                }
                parity_counts = {
                    value: int(count)
                    for value, count in session.execute(
                        select(tx.ShadowComparison.parity_class, func.count())
                        .join(
                            tx.ShadowEnvelope,
                            tx.ShadowEnvelope.envelope_id == tx.ShadowComparison.envelope_id,
                        )
                        .where(
                            tx.ShadowEnvelope.shadow_baseline_id
                            == baseline.shadow_baseline_id
                        )
                        .group_by(tx.ShadowComparison.parity_class)
                    )
                }
                open_gaps = int(
                    session.scalar(
                        select(func.count()).select_from(tx.ShadowGap).where(
                            tx.ShadowGap.shadow_baseline_id
                            == baseline.shadow_baseline_id,
                            tx.ShadowGap.state == "open",
                        )
                    )
                    or 0
                )
                last_sequence = session.scalar(
                    select(func.max(tx.ShadowEnvelope.rollout_sequence)).where(
                        tx.ShadowEnvelope.shadow_baseline_id
                        == baseline.shadow_baseline_id
                    )
                )
                normalized_delivery = {
                    key: delivery_counts.get(key, 0)
                    for key in ("pending", "claimed", "delivered", "failed")
                }
                normalized_parity = {
                    key: parity_counts.get(key, 0)
                    for key in ("exact", "semantic", "mismatch", "gap")
                }
                mismatch_count = normalized_parity["mismatch"]
                gap_count = open_gaps
                report["baseline"] = {
                    "shadow_baseline_id": str(baseline.shadow_baseline_id),
                    "generation_id": str(baseline.generation_id),
                    "source_generation_identity": baseline.source_generation_identity,
                    "source_commit": baseline.source_commit,
                    "status": baseline.status,
                    "delivery_counts": normalized_delivery,
                    "parity_counts": normalized_parity,
                    "open_gaps": open_gaps,
                    "last_rollout_sequence": (
                        None if last_sequence is None else int(last_sequence)
                    ),
                }
                epoch = session.scalar(
                    select(tx.ProjectionEpoch).where(
                        tx.ProjectionEpoch.generation_id == baseline.generation_id,
                        tx.ProjectionEpoch.status == "active",
                    )
                )
                report["projection_epoch"] = (
                    None
                    if epoch is None
                    else {
                        "projection_epoch_id": str(epoch.projection_epoch_id),
                        "external_effects_enabled": epoch.external_effects_enabled,
                    }
                )
        report["postgresql"] = {"state": "available"}
    except Exception as exc:
        report["postgresql"] = {
            "state": "unavailable",
            "reason": redact_reason(exc),
        }

    kill_switch = (
        {"state": "unavailable", "reason": "kill-switch path was not supplied"}
        if kill_switch_path is None
        else inspect_kill_switch(kill_switch_path)
    )
    report["kill_switch"] = kill_switch
    report["worker_unit"] = worker_unit

    lag_metric = 0.0 if backlog == 0 else oldest_age
    health_items = {
        "backlog": _threshold_health(
            backlog,
            warning=threshold_values.warning_backlog,
            critical=threshold_values.critical_backlog,
        ),
        "lag": _threshold_health(
            lag_metric,
            warning=threshold_values.warning_lag_seconds,
            critical=threshold_values.critical_lag_seconds,
        ),
        "capacity": _threshold_health(
            capacity_percent,
            warning=threshold_values.warning_capacity_percent,
            critical=threshold_values.critical_capacity_percent,
        ),
        "mismatches": _threshold_health(
            mismatch_count,
            warning=threshold_values.warning_mismatches,
            critical=threshold_values.critical_mismatches,
        ),
        "gaps": _threshold_health(
            gap_count,
            warning=threshold_values.warning_gaps,
            critical=threshold_values.critical_gaps,
        ),
        "kill_switch": {
            "state": (
                "healthy"
                if kill_switch["state"] == "clear"
                else (
                    "critical"
                    if kill_switch["state"] in {"engaged", "invalid"}
                    else "unavailable"
                )
            ),
            "value": kill_switch["state"],
        },
        "worker_unit": _worker_health(worker_unit),
    }
    required_health = [
        value
        for name, value in health_items.items()
        if name != "worker_unit" or worker_unit is not None
    ]
    report["health"] = {
        "state": _overall_health(required_health),
        "dimensions": health_items,
    }
    return report


def _add_threshold_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--warning-backlog", type=int)
    parser.add_argument("--critical-backlog", type=int)
    parser.add_argument("--warning-lag-seconds", type=float)
    parser.add_argument("--critical-lag-seconds", type=float)
    parser.add_argument("--warning-capacity-percent", type=float)
    parser.add_argument("--critical-capacity-percent", type=float)
    parser.add_argument("--warning-mismatches", type=int)
    parser.add_argument("--critical-mismatches", type=int)
    parser.add_argument("--warning-gaps", type=int)
    parser.add_argument("--critical-gaps", type=int)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-dark-launch")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "baseline-create"):
        child = sub.add_parser(name)
        child.add_argument("--database-url", required=True)
        child.add_argument("--spool-path", required=True, type=Path)
    status_p = sub.choices["status"]
    status_p.add_argument("--baseline-id", type=uuid.UUID)
    status_p.add_argument("--kill-switch", required=True, type=Path)
    status_p.add_argument("--worker-unit")
    status_p.add_argument("--systemctl-command", default="systemctl")
    status_p.add_argument(
        "--max-spool-bytes",
        type=int,
        default=int(os.environ.get("DISH_DARK_LAUNCH_MAX_SPOOL_BYTES", str(512 * 1024 * 1024))),
    )
    status_p.add_argument(
        "--max-spool-records",
        type=int,
        default=int(os.environ.get("DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS", "100000")),
    )
    status_p.add_argument(
        "--min-free-bytes",
        type=int,
        default=int(os.environ.get("DISH_DARK_LAUNCH_MIN_FREE_BYTES", str(1024 * 1024 * 1024))),
    )
    status_p.add_argument(
        "--busy-timeout-ms",
        type=int,
        default=int(os.environ.get("DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS", "50")),
    )
    _add_threshold_arguments(status_p)
    activate = sub.add_parser("activate-epoch")
    activate.add_argument("--database-url", required=True)
    activate.add_argument("--generation-id", required=True, type=uuid.UUID)
    activate.add_argument("--reason", required=True)
    create = sub.choices["baseline-create"]
    create.add_argument("--generation-id", required=True, type=uuid.UUID)
    create.add_argument("--source-generation", required=True)
    create.add_argument("--source-commit", required=True)
    disable = sub.add_parser("disable")
    disable.add_argument("--kill-switch", required=True, type=Path)
    disable.add_argument("--reason", required=True)
    enable = sub.add_parser("enable-capture")
    enable.add_argument("--kill-switch", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "disable":
        engage_kill_switch(
            args.kill_switch,
            {"disabled": True, "reason": args.reason, "at": _now().isoformat()},
        )
        return 0
    if args.command == "enable-capture":
        clear_kill_switch(args.kill_switch)
        return 0
    engine = create_database_engine(DatabaseSettings(url=args.database_url))
    factory = session_factory(engine)
    try:
        if args.command == "activate-epoch":
            with session_scope(factory) as session:
                epoch = ProjectionService(session).activate_epoch(
                    generation_id=args.generation_id,
                    activation_reason=args.reason,
                    created_at=_now(),
                    external_effects_enabled=False,
                )
                value = {
                    "projection_epoch_id": str(epoch.projection_epoch_id),
                    "status": epoch.status,
                    "external_effects_enabled": epoch.external_effects_enabled,
                }
        elif args.command == "baseline-create":
            with session_scope(factory) as session:
                baseline = ShadowService(session).create_baseline(
                    generation_id=args.generation_id,
                    source_generation_identity=args.source_generation,
                    source_commit=args.source_commit,
                    created_at=_now(),
                )
                value = {"shadow_baseline_id": str(baseline.shadow_baseline_id)}
        else:
            thresholds = StatusThresholds(
                warning_backlog=args.warning_backlog,
                critical_backlog=args.critical_backlog,
                warning_lag_seconds=args.warning_lag_seconds,
                critical_lag_seconds=args.critical_lag_seconds,
                warning_capacity_percent=args.warning_capacity_percent,
                critical_capacity_percent=args.critical_capacity_percent,
                warning_mismatches=args.warning_mismatches,
                critical_mismatches=args.critical_mismatches,
                warning_gaps=args.warning_gaps,
                critical_gaps=args.critical_gaps,
            )
            worker = None
            if args.worker_unit:
                try:
                    worker = observe_worker_unit(
                        unit_name=args.worker_unit,
                        systemctl_command=args.systemctl_command,
                    )
                    worker.pop("command", None)
                except Exception as exc:
                    worker = {
                        "state": "unavailable",
                        "unit_name": args.worker_unit,
                        "reason": redact_reason(exc),
                    }
            value = status(
                session_maker=factory,
                spool=ShadowSpool.open_existing_live_read_only(
                    args.spool_path,
                    busy_timeout_ms=args.busy_timeout_ms,
                    max_bytes=args.max_spool_bytes,
                    max_records=args.max_spool_records,
                    min_free_bytes=args.min_free_bytes,
                ),
                baseline_id=args.baseline_id,
                kill_switch_path=args.kill_switch,
                worker_unit=worker,
                thresholds=thresholds,
            )
        print(json.dumps(value, sort_keys=True, indent=2))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
