from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "lifecycle_economics_telemetry.py"
spec = importlib.util.spec_from_file_location("lifecycle_economics_telemetry", MODULE_PATH)
telemetry = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = telemetry
spec.loader.exec_module(telemetry)


def event(event_id: str, **overrides):
    value = {
        "schema_version": telemetry.SCHEMA_VERSION,
        "source": "github",
        "source_event_id": event_id,
        "observed_at": "2026-08-18T12:00:00Z",
        "series": "pr_flow",
        "repository": "marcogallotta/ai-tools",
        "task_id": "1217487779268948",
        "pr_number": 168,
        "lineage_id": "impl:1217487779268948",
        "generation_id": "gen-1",
        "head_sha": "a" * 40,
        "event_type": "implementation",
    }
    value.update(overrides)
    return value


class LifecycleEconomicsTelemetryTests(unittest.TestCase):
    def test_idempotency_and_retries_remain_distinct(self):
        a = event("e1", attempt_id="attempt-1", usage={"tool_calls": 2})
        b = event("e2", attempt_id="attempt-2", usage={"tool_calls": 3})
        records, report = telemetry.collect([a, a, b])
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["event_count"], 2)
        self.assertEqual(record["attempts"]["exact_ids"], ["attempt-1", "attempt-2"])
        self.assertEqual(record["usage"]["tool_calls"]["attributed_value"], 5)
        self.assertEqual(report["deduplicated_source_event_count"], 1)

    def test_replacement_generation_is_separate_and_linked(self):
        first = event("e1", generation_id="gen-1")
        second = event("e2", generation_id="gen-2", replaces_generation_id="gen-1")
        records, _ = telemetry.collect([first, second])
        self.assertEqual([r["generation_id"] for r in records], ["gen-1", "gen-2"])
        self.assertEqual(records[1]["replaces_generation_ids"], ["gen-1"])

    def test_unknown_identity_stays_unknown_and_does_not_collapse(self):
        a = event("e1", lineage_id=None, generation_id=None)
        b = event("e2", lineage_id=None, generation_id=None)
        records, _ = telemetry.collect([a, b])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["lineage_id"] == telemetry.UNKNOWN for r in records))
        self.assertTrue(all(r["generation_id"] == telemetry.UNKNOWN for r in records))

    def test_review_progress_requires_exact_source_attribution(self):
        events = [
            event("r1", review={"round_id": "round-1", "review_id": "review-1", "phase": "blocked", "exact_head_sha": "a" * 40}),
            event("r2", review={"round_id": "round-1", "review_id": "review-1", "phase": "fix_started", "exact_head_sha": "a" * 40}),
            event("r3", review={"round_id": "round-2", "review_id": "review-2", "phase": "rereview_requested", "exact_head_sha": "b" * 40}),
        ]
        record = telemetry.collect(events)[0][0]
        self.assertEqual(record["review"]["round_count"], 2)
        self.assertEqual(record["review"]["phase_counts"], {"blocked": 1, "fix_started": 1, "rereview_requested": 1})

    def test_human_metric_excludes_automatic_transitions(self):
        automatic = event("e1", operator={"required": False, "category": "manual_relay_or_queue_routing", "duration_ms": 500})
        human = event("e2", operator={"required": True, "category": "design_risk_product_decision", "action_id": "decision-1", "duration_ms": 1200})
        record = telemetry.collect([automatic, human])[0][0]
        self.assertEqual(record["operator"]["intervention_count"], 1)
        self.assertEqual(record["operator"]["category_counts"], {"design_risk_product_decision": 1})
        self.assertEqual(record["operator"]["category_duration_ms"], {"design_risk_product_decision": 1200})

    def test_override_recurrence_only_for_exact_gate_id(self):
        events = [
            event("o1", operator={"required": True, "category": "override_waiver_permission_prompt", "override": True, "gate_id": "bundle-witness"}),
            event("o2", operator={"required": True, "category": "override_waiver_permission_prompt", "override": True, "gate_id": "bundle-witness"}),
            event("o3", operator={"required": True, "category": "override_waiver_permission_prompt", "override": True}),
        ]
        record = telemetry.collect(events)[0][0]
        self.assertEqual(record["operator"]["override_count"], 3)
        self.assertEqual(record["operator"]["repeated_same_gate"], {"bundle-witness": 2})
        self.assertEqual(record["operator"]["unknown_override_gate_events"], 1)

    def test_ambiguous_tokens_and_cost_are_not_guessed_or_converted(self):
        events = [
            event("u1", usage={"total_tokens": 100, "cost": {"amount": "0.25", "currency": "USD", "unit": "provider_charge"}}),
            event("u2", usage={"total_tokens": telemetry.UNKNOWN, "cost": {"amount": telemetry.UNKNOWN, "currency": "EUR", "unit": "provider_charge"}}),
            event("u3", usage={"total_tokens": 20, "cost": {"amount": "0.10", "currency": "EUR", "unit": "provider_charge"}}),
        ]
        record = telemetry.collect(events)[0][0]
        self.assertEqual(record["usage"]["total_tokens"]["attributed_value"], 120)
        self.assertEqual(record["usage"]["total_tokens"]["unknown_event_count"], 1)
        self.assertEqual(record["usage"]["cost"]["exact_source_units"], [
            {"currency": "EUR", "unit": "provider_charge", "amount": "0.10"},
            {"currency": "USD", "unit": "provider_charge", "amount": "0.25"},
        ])
        self.assertEqual(record["usage"]["cost"]["unknown_event_count"], 1)

    def test_terminal_outcome_requires_authoritative_terminal_fact(self):
        speculative = event("e1", outcome={"kind": "merged", "authoritative": False, "terminal": True})
        nonterminal = event("e2", outcome={"kind": "merged", "authoritative": True, "terminal": False})
        authoritative = event("e3", outcome={"kind": "merged", "authoritative": True, "terminal": True})
        record = telemetry.collect([speculative, nonterminal, authoritative])[0][0]
        self.assertEqual(record["terminal_outcome"], "merged")

    def test_repository_health_does_not_inflate_pr_flow_diagnostics(self):
        flow = event("flow", timing={"stage": "integration", "duration_ms": 1000})
        health = event("health", series="repository_health", lineage_id="health", generation_id="run-1", timing={"stage": "full_regression", "duration_ms": 9000})
        records, report = telemetry.collect([flow, health])
        self.assertEqual(len(records), 2)
        self.assertEqual(report["pr_flow_generation_count"], 1)
        self.assertEqual(report["repository_health_event_count"], 1)
        self.assertIn("integration", report["timing"])
        self.assertNotIn("full_regression", report["timing"])

    def test_report_is_diagnostic_only_and_low_sample_visible(self):
        records, report = telemetry.collect([event("e1", timing={"stage": "review", "duration_ms": 100})])
        self.assertEqual(report["authority"], "diagnostic_only")
        self.assertEqual(report["eligibility"], telemetry.UNKNOWN)
        self.assertEqual(report["routing_recommendation"], telemetry.UNKNOWN)
        self.assertEqual(report["timing"]["review"]["p50_ms"], 100)
        self.assertEqual(report["timing"]["review"]["p90_ms"], 100)
        self.assertTrue(report["timing"]["review"]["low_sample"])

    def test_payload_text_is_rejected(self):
        for key in ("prompt", "chat", "content", "message", "body", "transcript", "source_code"):
            with self.subTest(key=key):
                with self.assertRaises(telemetry.TelemetryError):
                    telemetry.validate_event(event(f"payload-{key}", metadata={key: "do not retain this"}))

    def test_safe_append_failure_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "as-directory"
            directory.mkdir()
            result = telemetry.safe_append_event(directory, event("e1"))
        self.assertFalse(result["telemetry_written"])
        self.assertTrue(result["degraded"])
        self.assertIn("error", result)

    def test_cli_emit_degradation_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_path = root / "event.json"
            event_path.write_text(json.dumps(event("e1")), encoding="utf-8")
            target = root / "target-dir"
            target.mkdir()
            rc = telemetry.main(["emit", "--event-json", str(event_path), "--output", str(target)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
