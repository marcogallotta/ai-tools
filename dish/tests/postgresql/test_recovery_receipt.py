from __future__ import annotations

import json
import uuid

import pytest

from dish_pg.recovery_control import (
    RecoveredPhysicalState,
    RestoreControlError,
    load_restore_control,
)
from dish_pg.release import ALEMBIC_HEAD


def _physical_state() -> RecoveredPhysicalState:
    return RecoveredPhysicalState(
        database_name="dish_section2_source",
        system_identifier="7600000000000000000",
        schema_head=ALEMBIC_HEAD,
        backup_manifest_sha256="a" * 64,
        backup_evidence_sha256="b" * 64,
        recovery_timeline_id=2,
        recovery_target_type="lsn",
        recovery_target_lsn="0/200",
        recovery_completion_lsn="0/210",
        recovery_target_instance_sha256="c" * 64,
    )


def _payload(state: RecoveredPhysicalState) -> dict[str, object]:
    return {
        "external_control_id": "restore-1",
        "predecessor_generation_id": str(uuid.uuid4()),
        "generation_id": str(uuid.uuid4()),
        "bootstrap_id": str(uuid.uuid4()),
        "bootstrap_capability_sha256": "ab" * 32,
        "expected_database_name": state.database_name,
        "expected_system_identifier": state.system_identifier,
        "schema_head": ALEMBIC_HEAD,
        "dish_release": "dish@test",
        "honest_release": "honest@test",
        "protocol_release": "protocol@test",
        "openapi_release": "openapi@test",
        "routing_release": "routing@test",
        **{
            key: value
            for key, value in state.evidence_payload().items()
            if key not in {"database_name", "system_identifier", "schema_head"}
        },
        "recovery_evidence_sha256": state.evidence_sha256,
        "issued_at": "2026-08-06T08:00:00Z",
    }


def test_restore_control_file_requires_exact_physical_evidence(tmp_path):
    payload = {**_payload(_physical_state()), "unexpected": True}
    path = tmp_path / "control.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RestoreControlError, match="fields mismatch"):
        load_restore_control(path)
    with pytest.raises(RestoreControlError, match="unavailable"):
        load_restore_control(tmp_path / "missing.json")


@pytest.mark.parametrize("field", ["recovery_target_lsn", "recovery_completion_lsn"])
def test_restore_control_rejects_noncanonical_lsn(tmp_path, field):
    payload = _payload(_physical_state())
    payload[field] = "not-an-lsn"
    path = tmp_path / "control.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RestoreControlError, match="canonical PostgreSQL LSN"):
        load_restore_control(path)
