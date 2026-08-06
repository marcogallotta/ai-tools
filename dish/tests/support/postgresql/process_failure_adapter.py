"""Isolated deterministic external-effect adapter for §1 process rehearsals."""
from __future__ import annotations

import fcntl
import json
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from dish_pg.projection_worker import ExternalAttempt, ExternalObservation
from dish_pg.reconciliation_worker import ExternalCorpusItem, ReconciliationRecord
from dish_pg.workflow import sha256_json


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required rehearsal environment {name}")
    return value


def _barrier(label: str, payload: dict[str, Any] | None = None) -> None:
    socket_path = _required_env("DISH_SECTION1_BARRIER_SOCKET")
    message = {"label": label, "pid": os.getpid(), "payload": payload or {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall(json.dumps(message, sort_keys=True).encode("utf-8") + b"\n")
        received = b""
        while not received.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                raise RuntimeError(f"barrier {label!r} closed without release")
            received += chunk
    response = json.loads(received.decode("utf-8"))
    if response != {"action": "continue", "label": label}:
        raise RuntimeError(f"barrier {label!r} received invalid release {response!r}")


@contextmanager
def _ledger_lock() -> Iterator[tuple[Path, dict[str, Any]]]:
    ledger_path = Path(_required_env("DISH_SECTION1_EXTERNAL_LEDGER"))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if ledger_path.is_file():
            payload = json.loads(ledger_path.read_text(encoding="utf-8"))
        else:
            payload = {"dispatch_calls": 0, "recovery_observations": 0, "effects": {}}
        yield ledger_path, payload
        temporary = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, ledger_path)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _scenario() -> str:
    return os.environ.get("DISH_SECTION1_SCENARIO", "normal")


class DeterministicExternalAdapter:
    """Filesystem-backed external effect with process barriers and exact reread."""

    def __init__(self) -> None:
        if _scenario() == "before_claim":
            _barrier("before_claim")

    def prepare(self, claim) -> ExternalAttempt:
        if _scenario() == "after_claim":
            _barrier(
                "after_claim_before_durable_intent",
                {"event_id": str(claim.event_id), "claim_revision": claim.claim_revision},
            )
        return ExternalAttempt(
            request_identity=f"section1:{claim.event_id}",
            request_payload=dict(claim.payload),
            intended_external_id="123456789",
        )

    def attempt_and_observe(self, claim, attempt: ExternalAttempt) -> ExternalObservation:
        if _scenario() == "after_intent":
            _barrier(
                "after_durable_intent_before_external_call",
                {"event_id": str(claim.event_id), "dispatch_identity": attempt.request_identity},
            )
        identity = sha256_json(dict(attempt.request_payload))
        with _ledger_lock() as (_path, ledger):
            ledger["dispatch_calls"] += 1
            ledger["effects"][attempt.request_identity] = {
                "event_id": str(claim.event_id),
                "observed_identity": identity,
                "external_id": attempt.intended_external_id,
            }
        if _scenario() == "ambiguous_response":
            _barrier(
                "after_ambiguous_external_response_before_settlement",
                {"event_id": str(claim.event_id), "dispatch_identity": attempt.request_identity},
            )
        return _applied_observation(identity)

    def observe_recovery(self, claim, attempt: ExternalAttempt) -> ExternalObservation:
        with _ledger_lock() as (_path, ledger):
            ledger["recovery_observations"] += 1
            effect = ledger["effects"].get(attempt.request_identity)
        if _scenario() == "ambiguous_unresolved":
            return ExternalObservation(
                observed_applied=None,
                observed_identity=None,
                reread_complete=False,
                evidence={
                    "external_observation": {
                        "source": "external_reread",
                        "operation": claim.event_type,
                        "observed_external_id": attempt.intended_external_id,
                        "available": False,
                    }
                },
                decision_reason="isolated-adapter reread remained unavailable",
            )
        if effect is None:
            return ExternalObservation(
                observed_applied=False,
                observed_identity=None,
                reread_complete=True,
                evidence={
                    "external_observation": {
                        "source": "external_reread",
                        "operation": claim.event_type,
                        "observed_external_id": attempt.intended_external_id,
                        "observed_absent": True,
                    }
                },
                decision_reason="complete isolated-adapter reread found no effect",
            )
        return _applied_observation(str(effect["observed_identity"]), recovery=True)


def _applied_observation(identity: str, *, recovery: bool = False) -> ExternalObservation:
    return ExternalObservation(
        observed_applied=True,
        observed_identity=identity,
        reread_complete=True,
        evidence={
            "external_observation": {
                "source": "external_reread",
                "operation": "update_task_document",
                "observed_external_id": "123456789",
                "observed_document_identity": identity,
            }
        },
        decision_reason=(
            "isolated-adapter recovery reread confirmed effect"
            if recovery
            else "isolated-adapter post-call reread confirmed effect"
        ),
    )


def fetch_corpus(corpus_identity: str):
    if _scenario() == "reconciliation_before_transaction":
        _barrier("after_corpus_fetch_before_reconciliation_transaction")
    return (
        ExternalCorpusItem(
            item_identity=f"corpus:{corpus_identity}:item-1",
            entity_kind="task",
            payload={"external_id": "123456789"},
        ),
    )


def compare_item(_session, _generation_id, item: ExternalCorpusItem) -> ReconciliationRecord:
    return ReconciliationRecord(
        item_identity=item.item_identity,
        entity_kind=item.entity_kind,
        mapping_id=None,
        outcome="matched",
        evidence={"source": "isolated-section1-adapter", "payload": dict(item.payload)},
    )
