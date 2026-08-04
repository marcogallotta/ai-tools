"""Dark-launch operator controls and bounded status reporting."""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from dish_service.shadow_spool import ShadowSpool
from dish_service.path_safety import clear_kill_switch, engage_kill_switch

from . import models
from . import stage5_models as tx
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .transition import ProjectionService, ShadowService


def _now() -> datetime:
    return datetime.now(timezone.utc)


def status(*, session_maker, spool: ShadowSpool, baseline_id: uuid.UUID | None) -> dict[str, Any]:
    report: dict[str, Any] = {"spool": dict(spool.status())}
    with session_scope(session_maker) as session:
        generation = session.scalar(
            select(models.AuthorityGeneration).where(models.AuthorityGeneration.status == "active")
        )
        report["active_generation_id"] = None if generation is None else str(generation.generation_id)
        if baseline_id is None:
            baseline = session.scalar(
                select(tx.ShadowBaseline)
                .where(tx.ShadowBaseline.status == "open")
                .order_by(tx.ShadowBaseline.created_at.desc())
            )
        else:
            baseline = session.get(tx.ShadowBaseline, baseline_id)
        if baseline is None:
            report["baseline"] = None
            return report
        counts = {
            state: int(count)
            for state, count in session.execute(
                select(tx.ShadowDelivery.state, func.count())
                .join(tx.ShadowEnvelope, tx.ShadowEnvelope.envelope_id == tx.ShadowDelivery.envelope_id)
                .where(tx.ShadowEnvelope.shadow_baseline_id == baseline.shadow_baseline_id)
                .group_by(tx.ShadowDelivery.state)
            )
        }
        parity = {
            value: int(count)
            for value, count in session.execute(
                select(tx.ShadowComparison.parity_class, func.count())
                .join(tx.ShadowEnvelope, tx.ShadowEnvelope.envelope_id == tx.ShadowComparison.envelope_id)
                .where(tx.ShadowEnvelope.shadow_baseline_id == baseline.shadow_baseline_id)
                .group_by(tx.ShadowComparison.parity_class)
            )
        }
        open_gaps = int(session.scalar(
            select(func.count()).select_from(tx.ShadowGap).where(
                tx.ShadowGap.shadow_baseline_id == baseline.shadow_baseline_id,
                tx.ShadowGap.state == "open",
            )
        ) or 0)
        last_sequence = session.scalar(
            select(func.max(tx.ShadowEnvelope.rollout_sequence)).where(
                tx.ShadowEnvelope.shadow_baseline_id == baseline.shadow_baseline_id
            )
        )
        report["baseline"] = {
            "shadow_baseline_id": str(baseline.shadow_baseline_id),
            "generation_id": str(baseline.generation_id),
            "source_generation_identity": baseline.source_generation_identity,
            "source_commit": baseline.source_commit,
            "status": baseline.status,
            "delivery_counts": {key: counts.get(key, 0) for key in ("pending", "claimed", "delivered", "failed")},
            "parity_counts": {key: parity.get(key, 0) for key in ("exact", "semantic", "mismatch", "gap")},
            "open_gaps": open_gaps,
            "last_rollout_sequence": None if last_sequence is None else int(last_sequence),
        }
        epoch = session.scalar(
            select(tx.ProjectionEpoch).where(
                tx.ProjectionEpoch.generation_id == baseline.generation_id,
                tx.ProjectionEpoch.status == "active",
            )
        )
        report["projection_epoch"] = None if epoch is None else {
            "projection_epoch_id": str(epoch.projection_epoch_id),
            "external_effects_enabled": epoch.external_effects_enabled,
        }
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-dark-launch")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "baseline-create"):
        child = sub.add_parser(name)
        child.add_argument("--database-url", required=True)
        child.add_argument("--spool-path", required=True, type=Path)
    status_p = sub.choices["status"]
    status_p.add_argument("--baseline-id", type=uuid.UUID)
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
            value = status(
                session_maker=factory,
                spool=ShadowSpool.open_existing(
                    args.spool_path,
                    max_bytes=args.max_spool_bytes,
                    max_records=args.max_spool_records,
                    min_free_bytes=args.min_free_bytes,
                ),
                baseline_id=args.baseline_id,
            )
        print(json.dumps(value, sort_keys=True, indent=2))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
