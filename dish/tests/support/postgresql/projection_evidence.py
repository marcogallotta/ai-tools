"""Shared projection external-observation evidence."""
from __future__ import annotations

def external_evidence(identity: str | None = None) -> dict:
    fact = {"source": "external_reread", "operation": "update_task_document", "observed_external_id": "123456789"}
    if identity is not None:
        fact["observed_document_identity"] = identity
    return {"external_observation": fact}
