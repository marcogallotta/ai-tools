"""Shared declarative duty registry for lifecycle role consumers.

This module defines metadata only.  The existing host scheduler remains the
sole trigger owner and Lifecycle V4 remains the sole wake/receipt authority.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DutySpec:
    duty_id: str
    role: str
    schedule: str
    handler: str
    output_schema: str
    observe_only: bool = True


DUTIES = {
    "coordinator.hourly-frontier": DutySpec(
        "coordinator.hourly-frontier", "Coordinator", "hourly",
        "coordinator_hourly_frontier", "dish-coordinator-frontier-v1",
    ),
    "coordinator.noon-hygiene": DutySpec(
        "coordinator.noon-hygiene", "Coordinator", "12:00 Europe/Rome",
        "coordinator_noon_hygiene", "dish-coordinator-frontier-v1",
    ),
    "integrator.nightly-ci-consumer": DutySpec(
        "integrator.nightly-ci-consumer", "Integrator", "existing-host-nightly",
        "consume_existing_full_regression", "dish-integrator-observe-report-v1",
    ),
}


def duty_for(duty_id: str, *, role: str | None = None) -> DutySpec:
    try:
        duty = DUTIES[duty_id]
    except KeyError as exc:
        raise ValueError(f"unknown lifecycle duty: {duty_id}") from exc
    if role is not None and duty.role != role:
        raise ValueError(f"duty {duty_id} belongs to {duty.role}, not {role}")
    return duty
