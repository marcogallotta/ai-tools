from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from dish_pg.transition import ProjectionService
from dish_pg.workflow import sha256_json
from dish_pg.database import session_scope
from tests.support.postgresql.core import _bootstrap_registry, _import_one
from tests.support.postgresql.workflow import NOW, _claimed_execution, _next



def native_workflow_db(core_db):
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        task = _import_one(session, ids, context)
    return factory, ids, context, task.task_id

def projection(session, ids=None) -> ProjectionService:
    return ProjectionService(
        session,
        uuid_factory=(lambda: _next(ids)) if ids is not None else uuid.uuid4,
    )


def seed_events(factory, ids, context, task_id, *, count: int = 1) -> list[uuid.UUID]:
    with session_scope(factory) as session:
        service = projection(session, ids)
        service.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="attempt lifecycle regression",
            created_at=NOW,
            external_effects_enabled=True,
        )
        execution_id = _claimed_execution(session, ids, context, task_id)
        return [
            service.record(
                generation_id=context["generation_id"],
                execution_id=execution_id,
                task_id=task_id,
                event_type="update_task_document",
                payload={"content_version_id": f"v{index + 2}"},
                created_at=NOW + timedelta(seconds=index),
            )
            for index in range(count)
        ]


def external_evidence(
    *,
    available: bool = True,
    external_id: str = "123456789",
    observed_identity: str | None = None,
    absent: bool = False,
) -> dict:
    if not available:
        return {
            "external_observation": {
                "source": "unavailable",
                "operation": "update_task_document",
                "reason": "read timeout",
            }
        }
    fact = {
        "source": "external_reread",
        "operation": "update_task_document",
        "observed_external_id": external_id,
    }
    if absent:
        fact["observed_absent"] = True
    if observed_identity is not None:
        fact["observed_document_identity"] = observed_identity
    return {"external_observation": fact}


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def request_identity(request_payload) -> str:
    return sha256_json(dict(request_payload))
