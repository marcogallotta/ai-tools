"""Canonical valid evidence shape for runtime-wiring runner contract tests."""

from __future__ import annotations


def valid_scenario_evidence(identity: dict) -> dict:
    restart_worker = "section3-restart"
    original_worker = "section3-takeover-original"
    replacement_worker = "section3-takeover-replacement"
    authoritative_observation = {
        "kind": "marker_search",
        "observed_applied": True,
        "reread_complete": True,
        "evidence": {"external_observation": {"source": "external_marker_search"}},
    }
    restart = {
        "status": "passed",
        "worker_id": restart_worker,
        "before_process_death": {
            "process": {"pid": 201, "termination_state": "sigkill"},
            "snapshot": {"attempts": [{"state": "dispatched"}]},
            "external_ledger": {"dispatch_calls": 1},
        },
        "after_same_logical_worker_restart": {
            "process": {"pid": 202},
            "snapshot": {
                "attempts": [
                    {
                        "kind": "dispatch",
                        "state": "uncertain",
                        "worker_id": restart_worker,
                        "terminal": True,
                    },
                    {
                        "kind": "recovery",
                        "state": "confirmed",
                        "worker_id": restart_worker,
                        "terminal": True,
                    },
                ]
            },
            "external_ledger": {"dispatch_calls": 1, "recovery_observations": 1},
            "settlement": {
                "authoritative_external_observation": authoritative_observation,
                "settlement_adjudication": {"outcome": "confirmed"},
            },
        },
        "no_duplicate_after_post_settlement_restart": {
            "snapshot_unchanged": True,
            "external_ledger_unchanged": True,
        },
    }
    takeover = {
        "status": "passed",
        "original_worker_id": original_worker,
        "replacement_worker_id": replacement_worker,
        "original_claim": {
            "events": [
                {
                    "state": "claimed",
                    "claim_owner": original_worker,
                    "claim_revision": 1,
                }
            ]
        },
        "takeover_claim": {
            "events": [
                {
                    "state": "claimed",
                    "claim_owner": replacement_worker,
                    "claim_revision": 2,
                }
            ]
        },
        "final_settlement": {
            "attempts": [
                {
                    "kind": "dispatch",
                    "state": "confirmed",
                    "worker_id": replacement_worker,
                    "terminal": True,
                }
            ]
        },
        "external_ledger": {"dispatch_calls": 1, "recovery_observations": 0},
        "settlement": {
            "authoritative_external_observation": authoritative_observation,
            "settlement_adjudication": {"outcome": "confirmed"},
        },
        "no_duplicate_after_takeover_restart": {
            "snapshot_unchanged": True,
            "external_ledger_unchanged": True,
        },
    }
    return {
        "database": "dish_section3_runtime_wiring_test",
        "ports": {"private": 19001, "action": 19002, "postgresql_proxy": 19003},
        "service_health": {
            "profile": "test",
            "identity": identity,
            "isolation": {
                "asana_environment_keys": [],
                "bind_host": "127.0.0.1",
                "action_bind_host": "127.0.0.1",
                "supported_http_surfaces": ["action", "agent"],
            },
        },
        "command_boundary": {"result": {"ok": True}},
        "same_logical_worker_restart": restart,
        "different_worker_takeover": takeover,
        "stale_original_worker_rejection": {
            "status": "passed",
            "original_worker_id": original_worker,
            "replacement_worker_id": replacement_worker,
            "original_log_recorded_stale_claim_rejection": True,
            "snapshot_unchanged": True,
            "original_worker_attempt_count": 0,
        },
        "external_attempt_settlement": {
            "status": "passed",
            "same_logical_worker_restart": restart,
            "different_worker_takeover": takeover,
            "no_duplicate_dispatch_or_settlement": {
                "after_same_worker_restart": True,
                "after_takeover": True,
            },
        },
        "downstream_failure_projection": {
            "status": "passed",
            "projection_freshness": {"fresh": False, "state": "pending"},
            "snapshot": {
                "attempts": [{"state": "not_applied"}],
                "adjudications": [{"outcome": "not_applied"}],
            },
        },
        "unsupported_test_service_routes": {
            "status": "passed",
            "internal_error_count": 0,
            "routes": [
                {"status": 404, "payload": {"ok": False, "error": "not_found"}}
                for _ in range(4)
            ],
        },
        "reconciliation": {"ok": True},
        "postgresql_loss": {
            "health_while_down": {"ok": False},
            "health_after_restart": {"ok": True},
            "mutation_status": 503,
            "mutation_result": {
                "ok": False,
                "code": "BACKEND_REJECTED",
                "retryable": True,
                "errors": [{"rule": "postgresql_authority_unavailable"}],
            },
            "request_absent_after_restart": True,
            "task_absent_after_restart": True,
        },
        "runtime_identities": {},
    }
