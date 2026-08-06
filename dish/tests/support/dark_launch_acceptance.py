from __future__ import annotations

from typing import Any


def _summary(surface: str, operation_id: str, digest: str) -> dict[str, Any]:
    return {
        "surface": surface,
        "operation_id": operation_id,
        "observable_sha256": digest,
    }


def capture_report(*, mismatch: bool = False) -> dict[str, Any]:
    private_hash = "1" * 64
    action_hash = "2" * 64
    operation_id = "11111111-1111-1111-1111-111111111111"
    stable_probe = {
        "operation_id": operation_id,
        "stable": True,
        "surfaces": {
            "private": {
                "stable": True,
                "first": _summary("private", operation_id, private_hash),
                "second": _summary("private", operation_id, private_hash),
            },
            "action": {
                "stable": True,
                "first": _summary("action", operation_id, action_hash),
                "second": _summary("action", operation_id, action_hash),
            },
        },
    }
    observed_action_hash = "3" * 64 if mismatch else action_hash
    return {
        "tests": [
            {
                "name": "TEST 1 — live fail-open and latency",
                "status": "PASS",
                "evidence": {
                    "stable_operation_probes": [stable_probe],
                    "capture_on_requests": [
                        _summary("private", operation_id, private_hash),
                        _summary("action", operation_id, observed_action_hash),
                    ],
                    "broken_capture_request": _summary(
                        "action", operation_id, observed_action_hash
                    ),
                },
            },
            {
                "name": "TEST 2 — restart and spool conservation",
                "status": "PASS",
                "evidence": {
                    "accumulation_requests": [
                        _summary("private", operation_id, private_hash),
                        _summary("action", operation_id, action_hash),
                    ],
                    "post_restart_request": _summary(
                        "action", operation_id, action_hash
                    ),
                },
            },
            {
                "name": "TEST 3 — live kill switch",
                "status": "PASS",
                "evidence": {
                    "disabled_requests": [
                        _summary("private", operation_id, private_hash),
                        _summary("action", operation_id, action_hash),
                    ],
                    "resumed_requests": [
                        _summary("private", operation_id, private_hash),
                        _summary("action", operation_id, action_hash),
                    ],
                },
            },
        ],
        "restore": {
            "env_hash_matches": True,
            "service_restarted": True,
            "service_healthy": True,
            "errors": [],
        },
    }
