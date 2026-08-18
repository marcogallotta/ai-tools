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

    def test_missing_usage_and_execution_are_unknown_while_exact_zero_is_zero(self):
        missing = telemetry.collect([event("missing")])[0][0]
        self.assertEqual(missing["execution"], telemetry.UNKNOWN)
        self.assertEqual(missing["usage"]["total_tokens"]["attributed_value"], telemetry.UNKNOWN)
        self.assertEqual(missing["usage"]["cost"]["exact_source_units"], telemetry.UNKNOWN)

        zero = telemetry.collect([event(
            "zero",
            execution={"execution_id": "run-0", "host": "chatgpt", "provider": "openai", "model": "model-x", "config": "default", "snapshot": "snap-1"},
            usage={"tool_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": {"amount": "0", "currency": "USD", "unit": "provider_charge"}},
        )])[0][0]
        self.assertEqual(zero["usage"]["total_tokens"]["attributed_value"], 0)
        self.assertEqual(zero["usage"]["cost"]["exact_source_units"], [{"currency": "USD", "unit": "provider_charge", "amount": "0"}])

    def test_multi_provider_usage_never_cross_attributes(self):
        openai = event(
            "openai", attempt_id="impl-1",
            execution={"execution_id": "impl-1", "host": "chatgpt", "provider": "openai", "model": "gpt-x", "config": "implementation", "snapshot": "snap-a"},
            usage={"total_tokens": 100, "cost": {"amount": "0.50", "currency": "USD", "unit": "provider_charge"}},
        )
        review = event(
            "review", attempt_id="review-1",
            execution={"execution_id": "review-1", "host": "review-host", "provider": "provider-b", "model": "review-y", "config": "review", "snapshot": "snap-b"},
            usage={"total_tokens": 30, "cost": {"amount": "0.10", "currency": "EUR", "unit": "provider_charge"}},
            review={"round_id": "round-1", "review_id": "review-1", "phase": "passed", "exact_head_sha": "a" * 40},
        )
        record, report = telemetry.collect([openai, review])
        buckets = record[0]["execution_economics"]
        self.assertEqual(len(buckets), 2)
        by_provider = {bucket["identity"]["provider"]: bucket for bucket in buckets}
        self.assertEqual(by_provider["openai"]["usage"]["total_tokens"]["attributed_value"], 100)
        self.assertEqual(by_provider["provider-b"]["usage"]["total_tokens"]["attributed_value"], 30)
        self.assertEqual(by_provider["openai"]["usage"]["cost"]["exact_source_units"][0]["currency"], "USD")
        self.assertEqual(by_provider["provider-b"]["usage"]["cost"]["exact_source_units"][0]["currency"], "EUR")
        report_providers = {item["identity"]["provider"] for item in report["by_execution"]}
        self.assertEqual(report_providers, {"openai", "provider-b"})

    def test_repeated_exact_operator_action_is_counted_once(self):
        action = {
            "required": True,
            "category": "override_waiver_permission_prompt",
            "action_id": "override-action-1",
            "gate_id": "repository-bundle-witness",
            "override": True,
            "duration_ms": 500,
        }
        record = telemetry.collect([event("o1", operator=action), event("o2", operator=action)])[0][0]
        self.assertEqual(record["operator"]["intervention_count"], 1)
        self.assertEqual(record["operator"]["override_count"], 1)
        self.assertEqual(record["operator"]["category_duration_ms"], {"override_waiver_permission_prompt": 500})

    def test_closed_schema_rejects_arbitrary_top_level_and_nested_payload_fields(self):
        invalid = [
            event("top-text", text="payload"),
            event("top-description", description="payload"),
            event("top-diff", diff="payload"),
            event("top-raw", raw={"anything": "payload"}),
            event("nested-execution", execution={"host": "chatgpt", "metadata": {"text": "payload"}}),
            event("nested-usage", usage={"total_tokens": 1, "raw": "payload"}),
            event("nested-operator", operator={"required": False, "description": "payload"}),
        ]
        for value in invalid:
            with self.subTest(source_event_id=value["source_event_id"]):
                with self.assertRaises(telemetry.TelemetryError):
                    telemetry.validate_event(value)

    def test_authoritative_source_adapter_discards_bodies_and_preserves_exact_ids(self):
        head = "a" * 40
        lifecycle = {
            "number": 168,
            "head": head,
            "state": "changes_requested_fix_in_progress",
            "task_ids": ["1217487779268948"],
        }
        raw_pr = {
            "id": 5181601475, "number": 168, "state": "open",
            "updated_at": "2026-08-18T12:30:45Z",
            "head": {"sha": head}, "changed_files": 3, "additions": 824, "deletions": 0,
        }
        reviews = [{
            "id": 4960992929, "submitted_at": "2026-08-18T12:30:45Z",
            "commit_id": head,
            "body": "VERDICT: BLOCK\nGATE WAIVED BY MARCO OVERRIDE: repository-bundle witness",
        }]
        comments = [{
            "id": 99, "created_at": "2026-08-18T12:31:00Z",
            "body": f"<!-- dish-human-notice:v1 kind=local-implementation head={head} key=notice-1 -->\nHuman action notice recorded.",
        }]
        adapted = telemetry.events_from_authoritative_pr_sources(
            repository="marcogallotta/ai-tools", lifecycle=lifecycle, raw_pr=raw_pr, reviews=reviews, comments=comments,
        )
        self.assertEqual(len(adapted), 3)
        encoded = json.dumps(adapted)
        self.assertNotIn("VERDICT: BLOCK", encoded)
        self.assertNotIn("Human action notice recorded", encoded)
        review_event = next(item for item in adapted if item["source"] == "github-formal-review")
        self.assertEqual(review_event["review"]["review_id"], "4960992929")
        self.assertEqual(review_event["review"]["exact_head_sha"], head)
        self.assertEqual(review_event["operator"]["category"], "override_waiver_permission_prompt")
        self.assertEqual(review_event["execution"], telemetry.UNKNOWN)
        notice = next(item for item in adapted if item["source"] == "github-human-notice")
        self.assertEqual(notice["operator"]["action_id"], "notice-1")
        self.assertEqual(notice["operator"]["category"], "manual_relay_or_queue_routing")

    def test_safe_capture_pr_reads_authoritative_backend_and_remains_fail_open(self):
        head = "b" * 40
        class FakeGitHub:
            def __init__(self):
                self.calls = []
            def get_pr(self, number):
                self.calls.append(("pr", number))
                return {"id": 1, "number": number, "state": "open", "updated_at": "2026-08-18T12:00:00Z", "head": {"sha": head}}
            def get_reviews(self, number):
                self.calls.append(("reviews", number))
                return []
            def get_comments(self, number):
                self.calls.append(("comments", number))
                return []
        fake = FakeGitHub()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "events.jsonl"
            result = telemetry.safe_capture_pr(
                output, github=fake, repository="marcogallotta/ai-tools", pr_number=168,
                lifecycle={"number": 168, "head": head, "state": "review_ready", "task_ids": ["1217487779268948"]},
            )
            persisted = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(fake.calls, [("pr", 168), ("reviews", 168), ("comments", 168)])
        self.assertTrue(result["telemetry_written"])
        self.assertEqual(result["adapted_event_count"], 1)
        self.assertEqual(persisted[0]["source"], "dish-pr-lifecycle")

    def test_diagnostic_report_covers_outcomes_operator_cost_and_unknowns_by_execution(self):
        identity = {"execution_id": "impl-1", "host": "chatgpt", "provider": "openai", "model": "gpt-x", "config": "implementation", "snapshot": "snap-a"}
        merged = event(
            "merged", execution=identity, attempt_id="impl-1",
            usage={"total_tokens": 200, "cost": {"amount": "1.25", "currency": "USD", "unit": "provider_charge"}},
            operator={"required": True, "category": "design_risk_product_decision", "action_id": "decision-1", "duration_ms": 800},
            outcome={"kind": "merged", "authoritative": True, "terminal": True},
        )
        unknown = event("unknown-exec", generation_id="gen-2", execution=telemetry.UNKNOWN, usage=telemetry.UNKNOWN)
        _, report = telemetry.collect([merged, unknown])
        self.assertEqual(report["outcomes"]["counts"], {"UNKNOWN": 1, "merged": 1})
        self.assertEqual(report["operator"]["design_risk_product_decision"]["intervention_count"], 1)
        known_bucket = next(item for item in report["by_execution"] if item["identity"] != telemetry.UNKNOWN)
        self.assertEqual(known_bucket["identity"]["provider"], "openai")
        self.assertEqual(known_bucket["generation_outcomes"], {"merged": 1})
        self.assertEqual(known_bucket["usage"]["total_tokens"]["p50"], 200)
        self.assertEqual(known_bucket["cost"][0]["amount"]["p50"], "1.25")
        self.assertTrue(known_bucket["low_sample"])
        unknown_bucket = next(item for item in report["by_execution"] if item["identity"] == telemetry.UNKNOWN)
        self.assertGreaterEqual(unknown_bucket["usage"]["total_tokens"]["unknown_count"], 1)
        self.assertEqual(report["productivity_score"], telemetry.UNKNOWN)


    def test_estimate_error_reaches_report_and_missing_estimate_stays_unknown(self):
        exact = event("estimate-exact", timing={"stage": "implementation", "duration_ms": 1200, "estimate_ms": 1000})
        missing = event("estimate-missing", timing={"stage": "implementation", "duration_ms": 900})
        records, report = telemetry.collect([exact, missing])
        stage_record = records[0]["timing"]["stages"]["implementation"]
        self.assertEqual(stage_record["known_ms"], [1200, 900])
        self.assertEqual(stage_record["estimate_known_ms"], [1000])
        self.assertEqual(stage_record["estimate_unknown_event_count"], 1)
        self.assertEqual(stage_record["estimate_error_known_ms"], [200])
        self.assertEqual(stage_record["estimate_error_unknown_event_count"], 1)
        stage_report = report["timing"]["implementation"]
        self.assertEqual(stage_report["estimate_ms"]["p50_ms"], 1000)
        self.assertEqual(stage_report["estimate_ms"]["unknown_event_count"], 1)
        self.assertEqual(stage_report["estimate_error_ms"]["p50_ms"], 200)
        self.assertEqual(stage_report["estimate_error_ms"]["p90_ms"], 200)
        self.assertEqual(stage_report["estimate_error_ms"]["unknown_event_count"], 1)

    def test_unknown_attempts_do_not_become_zero_distribution_samples(self):
        identity = {"execution_id": "run-unknown", "host": "chatgpt", "provider": "openai", "model": "gpt-x", "config": "implementation", "snapshot": "snap-a"}
        record, report = telemetry.collect([event("unknown-attempt", execution=identity)])
        self.assertEqual(record[0]["attempts"]["exact_count"], 0)
        self.assertEqual(record[0]["attempts"]["unknown_event_count"], 1)
        flow_attempts = report["flow_economics"]["attempts"]
        self.assertEqual(flow_attempts["count"], 0)
        self.assertIsNone(flow_attempts["p50"])
        self.assertEqual(flow_attempts["unknown_count"], 1)
        self.assertEqual(flow_attempts["unknown_event_count"], 1)
        bucket = next(item for item in report["by_execution"] if item["identity"] != telemetry.UNKNOWN)
        self.assertEqual(bucket["attempts"]["count"], 0)
        self.assertIsNone(bucket["attempts"]["p50"])
        self.assertEqual(bucket["attempts"]["unknown_count"], 1)
        self.assertEqual(bucket["attempts"]["unknown_event_count"], 1)

    def test_override_adapter_requires_canonical_gate_identity(self):
        head = "c" * 40
        lifecycle = {"number": 168, "head": head, "state": "changes_requested_fix_in_progress", "task_ids": ["1217487779268948"]}
        raw_pr = {"id": 1, "number": 168, "state": "open", "updated_at": "2026-08-18T13:34:18Z", "head": {"sha": head}}
        reviews = [
            {
                "id": 10, "submitted_at": "2026-08-18T13:34:18Z", "commit_id": head,
                "body": "VERDICT: BLOCK\nGATE WAIVED BY MARCO OVERRIDE: repository-bundle witness for this Review/chat. Review used live connector-native evidence.",
            },
            {
                "id": 11, "submitted_at": "2026-08-18T13:35:18Z", "commit_id": head,
                "body": "VERDICT: BLOCK\nGATE WAIVED BY MARCO OVERRIDE: gate=repository-bundle-witness",
            },
        ]
        adapted = telemetry.events_from_authoritative_pr_sources(
            repository="marcogallotta/ai-tools", lifecycle=lifecycle, raw_pr=raw_pr, reviews=reviews, comments=[],
        )
        by_review = {item["review"]["review_id"]: item for item in adapted if item["source"] == "github-formal-review"}
        self.assertEqual(by_review["10"]["operator"]["gate_id"], telemetry.UNKNOWN)
        self.assertEqual(by_review["10"]["operator"]["action_id"], "github-review:10:override")
        self.assertEqual(by_review["11"]["operator"]["gate_id"], "repository-bundle-witness")

        recurrence = telemetry.collect([
            event("gate-1", operator={"required": True, "category": "override_waiver_permission_prompt", "action_id": "override-1", "gate_id": "repository-bundle-witness", "override": True}),
            event("gate-2", operator={"required": True, "category": "override_waiver_permission_prompt", "action_id": "override-2", "gate_id": "repository-bundle-witness", "override": True}),
        ])[0][0]
        self.assertEqual(recurrence["operator"]["repeated_same_gate"], {"repository-bundle-witness": 2})

    def test_source_execution_scopes_local_attempt_and_action_ids(self):
        exec_a = {"execution_id": "run-a", "host": "chatgpt", "provider": "provider-a", "model": "m-a", "config": "implementation", "snapshot": "s-a"}
        exec_b = {"execution_id": "run-b", "host": "review-host", "provider": "provider-b", "model": "m-b", "config": "review", "snapshot": "s-b"}
        operator = {"required": True, "category": "manual_relay_or_queue_routing", "action_id": "123", "duration_ms": 10}
        a = event("scope-a", source="github", execution=exec_a, attempt_id="1", operator=operator)
        a_repeat = event("scope-a-repeat", source="github", execution=exec_a, attempt_id="1", operator=operator)
        b = event("scope-b", source="worker", execution=exec_b, attempt_id="1", operator=operator)
        record, report = telemetry.collect([a, a_repeat, b])
        generation = record[0]
        self.assertEqual(generation["attempts"]["exact_count"], 2)
        self.assertEqual(generation["attempts"]["exact_ids"], ["1", "1"])
        self.assertEqual(len(generation["attempts"]["exact_scoped_ids"]), 2)
        self.assertEqual(generation["operator"]["intervention_count"], 2)
        self.assertEqual(generation["operator"]["action_ids"], ["123", "123"])
        self.assertEqual(len(generation["operator"]["action_scoped_ids"]), 2)
        providers = {item["identity"]["provider"]: item for item in report["by_execution"] if item["identity"] != telemetry.UNKNOWN}
        self.assertEqual(providers["provider-a"]["attempts"]["p50"], 1)
        self.assertEqual(providers["provider-a"]["operator_interventions"]["p50"], 1)
        self.assertEqual(providers["provider-b"]["attempts"]["p50"], 1)
        self.assertEqual(providers["provider-b"]["operator_interventions"]["p50"], 1)


if __name__ == "__main__":
    unittest.main()
