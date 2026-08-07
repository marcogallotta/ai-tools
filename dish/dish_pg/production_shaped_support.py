"""Local-only adapters for the Section 4 production-shaped rehearsal.

The module deliberately has no network client.  It reads one digest-bound
sanitized NDJSON corpus and writes projection observations only beneath a path
embedded by the owning rehearsal process.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from . import stage5_models as projection
from .production_shaped_runtime import reach_barrier
from .projection_worker import ExternalAttempt, ExternalObservation
from .reconciliation_worker import ExternalCorpusItem, ReconciliationRecord
from .transition import ProjectionClaim
from .workflow import sha256_json

CORPUS_PREFIX = "dish-sanitized-ndjson-v1|"
EVIDENCE_MARKER = ".dish-section4-evidence"
EVIDENCE_SCHEMA = "dish-postgresql-production-shaped-rehearsal-v1"


def _barrier(label: str, payload: Mapping[str, Any] | None = None) -> None:
    raw = os.environ.get("DISH_SECTION4_BARRIER_SOCKET", "").strip()
    if not raw:
        raise RuntimeError(f"Section 4 scenario requires barrier {label!r}")
    reach_barrier(Path(raw), label, payload)


@contextmanager
def _effect_ledger():
    raw = os.environ.get("DISH_SECTION4_EFFECT_LEDGER", "").strip()
    if not raw:
        yield None
        return
    path = Path(raw).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            value = {"dispatch_calls": 0, "recovery_observations": 0, "effects": {}}
        yield value
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _projection_scenario() -> str:
    return os.environ.get("DISH_SECTION4_PROJECTION_SCENARIO", "normal").strip() or "normal"


def _reconciliation_scenario() -> str:
    return os.environ.get("DISH_SECTION4_RECONCILIATION_SCENARIO", "normal").strip() or "normal"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def corpus_identity(path: Path, digest: str) -> str:
    resolved = path.expanduser().resolve()
    if "|" in str(resolved):
        raise ValueError("corpus path may not contain '|'")
    return f"{CORPUS_PREFIX}{digest}|{resolved}"


def parse_corpus_identity(value: str) -> tuple[Path, str]:
    if not value.startswith(CORPUS_PREFIX):
        raise ValueError("unsupported corpus identity")
    remainder = value[len(CORPUS_PREFIX) :]
    try:
        digest, raw_path = remainder.split("|", 1)
    except ValueError as exc:
        raise ValueError("corpus identity is incomplete") from exc
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("corpus identity digest is not canonical SHA-256")
    path = Path(raw_path).expanduser().resolve()
    if sha256_file(path) != digest:
        raise ValueError("corpus identity digest mismatch")
    return path, digest


def fetch_sanitized_corpus(identity: str) -> tuple[ExternalCorpusItem, ...]:
    path, digest = parse_corpus_identity(identity)
    items: list[ExternalCorpusItem] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError(f"corpus line {line_number} is not an object")
            task_id = str(value.get("task_id", ""))
            if not task_id:
                raise ValueError(f"corpus line {line_number} omits task_id")
            items.append(
                ExternalCorpusItem(
                    item_identity=f"task:{task_id}:{digest}",
                    entity_kind="task",
                    payload=value,
                )
            )
    if not items:
        raise ValueError("sanitized corpus contains no records")
    if _reconciliation_scenario() == "after_fetch_before_transaction":
        _barrier(
            "reconciliation_after_fetch_before_transaction",
            {"corpus_items": len(items), "corpus_sha256": digest},
        )
    return tuple(items)


def compare_sanitized_item(
    session: Session,
    generation_id: uuid.UUID,
    item: ExternalCorpusItem,
) -> ReconciliationRecord:
    payload = dict(item.payload)
    task_id = uuid.UUID(str(payload["task_id"]))
    expected_gid = str(payload["asana_task_gid"])
    failures: list[str] = []

    task = session.get(models.DishTask, task_id)
    if task is None or task.creation_route != "import":
        failures.append("task authority missing or non-imported")
    alias = session.scalar(
        select(models.TaskExternalAlias).where(
            models.TaskExternalAlias.task_id == task_id,
            models.TaskExternalAlias.external_system == "asana",
            models.TaskExternalAlias.external_id == expected_gid,
            models.TaskExternalAlias.state == "active",
        )
    )
    if alias is None:
        failures.append("active sanitized alias missing")
    content = session.scalar(
        select(models.ContentVersion).where(
            models.ContentVersion.generation_id == generation_id,
            models.ContentVersion.task_id == task_id,
            models.ContentVersion.content_identity == str(payload["content_identity"]),
        )
    )
    if content is None or content.title != payload["title"] or content.body != payload["body"]:
        failures.append("content authority mismatch")
    placement = session.get(models.CurrentTaskSectionPlacement, (generation_id, task_id))
    if placement is None or str(placement.section_id) != str(payload["section_id"]):
        failures.append("section placement mismatch")
    completion = session.get(models.CurrentTaskCompletion, (generation_id, task_id))
    if completion is None or bool(completion.completed) is not bool(payload["completed"]):
        failures.append("completion mismatch")
    mapping = session.scalar(
        select(projection.TaskProjectionMapping).where(
            projection.TaskProjectionMapping.generation_id == generation_id,
            projection.TaskProjectionMapping.task_id == task_id,
            projection.TaskProjectionMapping.state == "active",
        )
    )
    if mapping is None:
        failures.append("active task projection mapping missing")

    return ReconciliationRecord(
        item_identity=item.item_identity,
        entity_kind="task",
        mapping_id=None if mapping is None else mapping.mapping_id,
        outcome="blocked" if failures else "matched",
        evidence={
            "schema": "dish-section4-sanitized-comparison-v1",
            "task_id": str(task_id),
            "failures": failures,
            "sanitized": True,
            "external_io": False,
        },
    )


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _owned_evidence_path(raw_path: object) -> Path:
    path = Path(str(raw_path)).expanduser().resolve()
    marker = path.parent / EVIDENCE_MARKER
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("local projection path is not beneath owned Section 4 evidence") from exc
    if value != {"schema": EVIDENCE_SCHEMA}:
        raise ValueError("local projection evidence ownership marker is invalid")
    return path


class LocalProjectionAdapter:
    """Projection adapter that can only materialize a local JSON observation."""

    def prepare(self, claim: ProjectionClaim) -> ExternalAttempt:
        store = _owned_evidence_path(claim.payload["local_store_path"])
        request_payload: dict[str, Any] = {
            "store_path": str(store),
            "task_id": str(claim.task_id),
            "event_id": str(claim.event_id),
            "event_type": claim.event_type,
            "intent_sha256": sha256_json(dict(claim.payload)),
        }
        authoritative_snapshot = claim.payload.get("authoritative_snapshot")
        if claim.event_type == "reproject" and isinstance(authoritative_snapshot, Mapping):
            request_payload["projected_state"] = dict(authoritative_snapshot)
        return ExternalAttempt(
            request_identity=f"section4-local:{claim.idempotency_key}",
            request_payload=request_payload,
            intended_external_id=f"local-task:{claim.task_id}",
        )

    def attempt_and_observe(
        self, claim: ProjectionClaim, attempt: ExternalAttempt
    ) -> ExternalObservation:
        scenario = _projection_scenario()
        if scenario == "before_effect":
            _barrier(
                "projection_after_intent_before_effect",
                {"event_id": str(claim.event_id), "request_identity": attempt.request_identity},
            )
        store = _owned_evidence_path(attempt.request_payload["store_path"])
        projected: dict[str, Any] = {
            "schema": "dish-section4-local-projection-v1",
            "request_identity": attempt.request_identity,
            "task_id": str(claim.task_id),
            "event_id": str(claim.event_id),
            "event_type": claim.event_type,
            "intent_sha256": attempt.request_payload["intent_sha256"],
        }
        if "projected_state" in attempt.request_payload:
            projected["projected_state"] = attempt.request_payload["projected_state"]
        _atomic_json(store, projected)
        with _effect_ledger() as ledger:
            if ledger is not None:
                ledger["dispatch_calls"] = int(ledger.get("dispatch_calls", 0)) + 1
                effects = ledger.setdefault("effects", {})
                effects[attempt.request_identity] = {
                    "event_id": str(claim.event_id),
                    "store_sha256": sha256_file(store),
                }
        if scenario == "after_effect":
            _barrier(
                "projection_after_effect_before_observation",
                {"event_id": str(claim.event_id), "request_identity": attempt.request_identity},
            )
        return self._observe(claim, attempt)

    def observe_recovery(
        self, claim: ProjectionClaim, attempt: ExternalAttempt
    ) -> ExternalObservation:
        with _effect_ledger() as ledger:
            if ledger is not None:
                ledger["recovery_observations"] = int(
                    ledger.get("recovery_observations", 0)
                ) + 1
        return self._observe(claim, attempt)

    @staticmethod
    def _observe(claim: ProjectionClaim, attempt: ExternalAttempt) -> ExternalObservation:
        store = _owned_evidence_path(attempt.request_payload["store_path"])
        evidence: dict[str, Any] = {
            "schema": "dish-section4-local-projection-observation-v1",
            "store_path": str(store),
            "external_io": False,
        }
        fact: dict[str, Any] = {
            "source": "external_reread",
            "operation": claim.event_type,
        }
        evidence["external_observation"] = fact
        if not store.is_file():
            fact["observed_external_id"] = attempt.intended_external_id
            fact["observed_absent"] = True
            return ExternalObservation(
                observed_applied=False,
                observed_identity=None,
                reread_complete=True,
                evidence=evidence,
                decision_reason="local projection store reread proves absence",
            )
        try:
            observed = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fact["reason"] = f"malformed local projection observation: {type(exc).__name__}"
            return ExternalObservation(
                observed_applied=None,
                observed_identity=None,
                reread_complete=False,
                evidence=evidence,
                decision_reason="local projection store reread was not usable",
            )
        evidence["store_sha256"] = sha256_file(store)
        if not isinstance(observed, Mapping):
            fact["reason"] = "local projection observation is not an object"
            return ExternalObservation(
                observed_applied=None,
                observed_identity=None,
                reread_complete=False,
                evidence=evidence,
                decision_reason="local projection store reread was not usable",
            )
        observed_task_id = str(observed.get("task_id") or "").strip()
        if observed_task_id:
            fact["observed_external_id"] = f"local-task:{observed_task_id}"
        if (
            observed.get("schema") != "dish-section4-local-projection-v1"
            or observed.get("event_type") != claim.event_type
        ):
            fact["reason"] = "local projection observation schema or operation mismatch"
            return ExternalObservation(
                observed_applied=None,
                observed_identity=None,
                reread_complete=True,
                evidence=evidence,
                decision_reason="local projection store reread did not prove the intended operation",
            )
        observed_identity = None
        if claim.event_type == "reproject":
            projected_state = observed.get("projected_state")
            if isinstance(projected_state, Mapping):
                observed_identity = sha256_json(dict(projected_state))
                fact["observed_reproject_state_identity"] = observed_identity
        return ExternalObservation(
            observed_applied=True if observed_identity is not None else None,
            observed_identity=observed_identity,
            reread_complete=True,
            evidence=evidence,
            decision_reason="local projection store independent state reread",
        )
