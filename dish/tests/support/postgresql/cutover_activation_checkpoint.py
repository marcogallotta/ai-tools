"""Process-boundary helpers for the Stage 6 cutover activation rehearsal."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg import stage6_models as rel
from dish_pg.process_failure_rehearsal import notify_process_barrier, write_json_atomic
from dish_service.legacy_writer_fence import read_legacy_writer_fence
from tests.support.postgresql.process_failure import BarrierServer, _start_child

CHECKPOINT_CHILD_FORMAT = "dish-cutover-checkpoint-probe-v1"
STALE_WRITER_CHILD_FORMAT = "dish-stale-legacy-writer-probe-v1"


def _snapshot(*, dsn: str, cutover_run_id: uuid.UUID, generation_id: uuid.UUID) -> dict:
    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            run = session.get(rel.CutoverRun, cutover_run_id)
            if run is None:
                raise AssertionError(f"cutover run {cutover_run_id} is absent")
            checkpoints = session.scalars(
                select(rel.CutoverCheckpoint)
                .where(rel.CutoverCheckpoint.cutover_run_id == cutover_run_id)
                .order_by(rel.CutoverCheckpoint.sequence)
            ).all()
            admission = session.get(rel.MutationAdmissionControl, generation_id)
            activation_count = session.scalar(
                select(func.count())
                .select_from(models.AuthorityActivation)
                .where(
                    models.AuthorityActivation.generation_id == generation_id,
                    models.AuthorityActivation.outcome == "activated",
                )
            )
            return {
                "cutover_run_id": str(run.cutover_run_id),
                "state": run.state,
                "state_revision": int(run.state_revision),
                "terminal": run.terminal_at is not None,
                "checkpoints": [
                    {
                        "sequence": int(row.sequence),
                        "kind": row.checkpoint_kind,
                        "payload_sha256": row.payload_sha256,
                    }
                    for row in checkpoints
                ],
                "mutation_admission": None
                if admission is None
                else {
                    "state": admission.state,
                    "control_revision": int(admission.control_revision),
                    "opened": admission.opened_at is not None,
                },
                "authority_activation_count": int(activation_count or 0),
            }
    finally:
        engine.dispose()


def start_checkpoint_probe(
    *,
    dsn: str,
    tmp_path: Path,
    cutover_run_id: uuid.UUID,
    generation_id: uuid.UUID,
    expected_state: str,
    output: Path,
    barrier: BarrierServer | None,
    label: str,
):
    command = [
        sys.executable,
        "-m",
        "tests.support.postgresql.cutover_activation_checkpoint",
        "checkpoint",
        "--dsn",
        dsn,
        "--cutover-run-id",
        str(cutover_run_id),
        "--generation-id",
        str(generation_id),
        "--expected-state",
        expected_state,
        "--output",
        str(output),
    ]
    if barrier is not None:
        command.extend(["--barrier-label", label])
    return _start_child(
        command,
        tmp_path=tmp_path,
        barrier=barrier,
        ledger=tmp_path / "unused-cutover-checkpoint-ledger.json",
        scenario="cutover-activation-checkpoint",
        label=label,
    )


def assert_checkpoint_survives_process_death(
    *,
    dsn: str,
    tmp_path: Path,
    cutover_run_id: uuid.UUID,
    generation_id: uuid.UUID,
    expected_state: str,
) -> dict:
    label = f"cutover-{expected_state}"
    killed_output = tmp_path / f"{label}-killed.json"
    with BarrierServer() as barrier:
        child = start_checkpoint_probe(
            dsn=dsn,
            tmp_path=tmp_path,
            cutover_run_id=cutover_run_id,
            generation_id=generation_id,
            expected_state=expected_state,
            output=killed_output,
            barrier=barrier,
            label=label,
        )
        reached = barrier.wait(label)
        killed_snapshot = dict(reached.payload["snapshot"])
        exit_code = child.kill()
        reached.close()
    assert exit_code != 0
    assert json.loads(killed_output.read_text(encoding="utf-8"))["snapshot"] == killed_snapshot

    recovered_output = tmp_path / f"{label}-recovered.json"
    replacement = start_checkpoint_probe(
        dsn=dsn,
        tmp_path=tmp_path,
        cutover_run_id=cutover_run_id,
        generation_id=generation_id,
        expected_state=expected_state,
        output=recovered_output,
        barrier=None,
        label=f"{label}-recovery",
    )
    replacement.wait()
    recovered = json.loads(recovered_output.read_text(encoding="utf-8"))["snapshot"]
    assert recovered == killed_snapshot
    return {
        "state": expected_state,
        "terminated_process_exit_code": exit_code,
        "snapshot": recovered,
        "recovery_equal": True,
    }


def start_stale_writer_probe(
    *,
    fence_path: Path,
    tmp_path: Path,
    output: Path,
    barrier: BarrierServer,
):
    return _start_child(
        [
            sys.executable,
            "-m",
            "tests.support.postgresql.cutover_activation_checkpoint",
            "stale-writer",
            "--fence-path",
            str(fence_path),
            "--output",
            str(output),
        ],
        tmp_path=tmp_path,
        barrier=barrier,
        ledger=tmp_path / "unused-stale-writer-ledger.json",
        scenario="stale-writer-fence",
        label="stale-legacy-writer",
    )


def _checkpoint_main(args: argparse.Namespace) -> int:
    snapshot = _snapshot(
        dsn=args.dsn,
        cutover_run_id=uuid.UUID(args.cutover_run_id),
        generation_id=uuid.UUID(args.generation_id),
    )
    if snapshot["state"] != args.expected_state:
        raise AssertionError(
            f"expected cutover state {args.expected_state!r}, observed {snapshot['state']!r}"
        )
    payload = {"format": CHECKPOINT_CHILD_FORMAT, "snapshot": snapshot}
    write_json_atomic(args.output, payload)
    if args.barrier_label:
        notify_process_barrier(args.barrier_label, {"snapshot": snapshot})
    return 0


def _stale_writer_main(args: argparse.Namespace) -> int:
    path = args.fence_path.resolve()
    if os.path.lexists(path):
        raise AssertionError("stale-writer probe must start before fence engagement")
    notify_process_barrier("stale_process_ready", {"fence_path": str(path)})
    manifest, digest = read_legacy_writer_fence(path)
    rejected = manifest is not None
    payload = {
        "format": STALE_WRITER_CHILD_FORMAT,
        "fence_path": str(path),
        "rejected": rejected,
        "manifest_format": None if manifest is None else manifest.get("format"),
        "manifest_sha256": digest,
    }
    write_json_atomic(args.output, payload)
    if not rejected:
        raise AssertionError("process started before engagement did not observe the writer fence")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--dsn", required=True)
    checkpoint.add_argument("--cutover-run-id", required=True)
    checkpoint.add_argument("--generation-id", required=True)
    checkpoint.add_argument("--expected-state", required=True)
    checkpoint.add_argument("--output", type=Path, required=True)
    checkpoint.add_argument("--barrier-label")
    stale = sub.add_parser("stale-writer")
    stale.add_argument("--fence-path", type=Path, required=True)
    stale.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "checkpoint":
        return _checkpoint_main(args)
    if args.mode == "stale-writer":
        return _stale_writer_main(args)
    raise AssertionError(args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
