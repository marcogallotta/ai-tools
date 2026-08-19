import datetime
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "dish-asana-migration-plan"
SPEC = importlib.util.spec_from_loader("migration_planner", SourceFileLoader("migration_planner", str(SCRIPT)))
assert SPEC and SPEC.loader
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)


def comment(*items):
    history = [
        {"created_at": created_at, "text": text}
        for created_at, text in sorted(items, reverse=True)
    ]
    if not history:
        return None
    return {**history[0], "history": history}


def task(**overrides):
    value = {
        "gid": "9999999999999999",
        "name": "A stable subject",
        "notes": "",
        "completed": False,
        "modified_at": "2026-08-19T12:00:00.000Z",
        "current_sections": ["Backlog"],
        "custom_fields": [],
        "comment": None,
    }
    value.update(overrides)
    return value


class PlannerClassificationTests(unittest.TestCase):
    def classify(self, value, prs=None):
        return planner.classify_target(value, prs or [], {})

    def test_priority_never_comes_from_title(self):
        value, source = planner.current_priority("", None, [])
        self.assertEqual((value, source), ("UNSET", "no authoritative priority record"))
        value = task(name="P-CRITICAL — urgent words only")
        self.assertEqual(planner.current_priority(value["notes"], value["comment"], [])[0], "UNSET")

    def test_newest_priority_revocation_wins(self):
        evidence = comment(
            ("2026-08-19T11:00:00.000Z", "Marco explicitly REVOKED P-CRITICAL priority."),
            ("2026-08-18T11:00:00.000Z", "PRIORITY: P-CRITICAL"),
        )
        self.assertEqual(planner.current_priority("", evidence, [])[0], "UNSET")

    def test_completed_is_done_even_with_old_residual_words(self):
        value = task(completed=True, notes="POST-MERGE ROLLOUT REQUIRED")
        self.assertEqual(self.classify(value)[0], "Done")

    def test_handoff_sent_is_ready_not_running(self):
        evidence = comment((
            "2026-08-19T11:00:00.000Z",
            "COORDINATOR DISPATCH RECORD\nSENT / USER RELAYED\nAcceptance NOT YET PROVEN",
        ))
        value = task(comment=evidence)
        target, confidence, _ = self.classify(value)
        self.assertEqual((target, confidence), ("Ready", "high"))
        self.assertEqual(planner.handoff_evidence(evidence), (True, "2026-08-19T11:00:00.000Z"))

    def test_handoff_age_is_objective(self):
        now = datetime.datetime(2026, 8, 19, 14, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(planner.age_hours("2026-08-19T11:00:00.000Z", now), 3.0)

    def test_open_canonical_owning_pr_proves_development(self):
        gid = "1217000000000000"
        value = task(gid=gid)
        prs = [{
            "number": 201,
            "state": "open",
            "body": f"dish-owning-task:v1 task={gid}",
        }]
        self.assertEqual(self.classify(value, prs)[0], "Under Development")

    def test_historical_human_request_does_not_override_resolution(self):
        evidence = comment(
            ("2026-08-19T11:00:00.000Z", "MARCO APPROVED — HUMAN DECISION RESOLVED"),
            ("2026-08-18T11:00:00.000Z", "NEEDS MARCO INPUT"),
        )
        value = task(comment=evidence)
        self.assertNotEqual(self.classify(value)[0], "Needs Human Review")

    def test_incidental_human_policy_prose_is_not_task_state(self):
        evidence = comment((
            "2026-08-19T11:00:00.000Z",
            "AUDIT ADDENDUM\nA future genuine HUMAN DECISION REQUIRED must be recorded durably.",
        ))
        value = task(notes="STATE: OPERATIONAL SETUP\nHUMAN DECISION BEFORE IMPLEMENTATION: NONE", comment=evidence)
        self.assertNotEqual(self.classify(value)[0], "Needs Human Review")

    def test_newest_marco_override_resolves_older_final_stamp(self):
        evidence = comment(
            ("2026-08-19T11:00:00.000Z", "MARCO OVERRIDE — proceed\nGATE WAIVED BY MARCO OVERRIDE"),
            ("2026-08-19T10:00:00.000Z", "DISPOSITION: Ready for Marco final stamp"),
        )
        value = task(comment=evidence)
        self.assertNotEqual(self.classify(value)[0], "Needs Human Review")

    def test_formal_pass_can_be_invalidated_by_later_revised_state(self):
        evidence = comment(("2026-08-19T10:00:00.000Z", "AGENT REVIEW — PASS\nVERDICT: IMPLEMENTATION READY"))
        value = task(
            notes="STATE: AMENDED — AGENT RE-REVIEW REQUIRED",
            comment=evidence,
            modified_at="2026-08-19T11:00:00.000Z",
        )
        self.assertEqual(self.classify(value)[0], "Needs Agentic Review")

    def test_review_pass_without_next_disposition_stays_for_semantic_triage(self):
        evidence = comment(("2026-08-19T10:00:00.000Z", "FOCUSED DESIGN RE-REVIEW — PASS\nVERDICT: PASS"))
        value = task(comment=evidence, current_sections=["Review / Integration"])
        target, confidence, reason = self.classify(value)
        self.assertEqual((target, confidence), ("Needs Processing", "low"))
        self.assertIn("no durable record", reason)

    def test_review_pass_plus_ready_section_is_ready(self):
        evidence = comment(("2026-08-19T10:00:00.000Z", "FOCUSED DESIGN REVIEW — PASS\nVERDICT: PASS"))
        value = task(comment=evidence, current_sections=["Ready"])
        self.assertEqual(self.classify(value)[0], "Ready")

    def test_structured_marco_design_decision_in_review_headline_is_human_review(self):
        evidence = comment((
            "2026-08-19T10:00:00.000Z",
            "INDEPENDENT DESIGN RE-REVIEW — PASS / MARCO DESIGN DECISION REQUIRED",
        ))
        value = task(comment=evidence, current_sections=["Blocked / Decision"])
        self.assertEqual(self.classify(value)[0], "Needs Human Review")

    def test_review_block_routes_to_research(self):
        evidence = comment(("2026-08-19T11:00:00.000Z", "AGENT REVIEW — BLOCK"))
        value = task(comment=evidence)
        self.assertEqual(self.classify(value)[0], "Needs Research")

    def test_title_review_words_do_not_route_task(self):
        value = task(name="AGENT REVIEW REQUIRED — title-only residue")
        self.assertEqual(self.classify(value)[0], "Needs Processing")

    def test_semantic_override_requires_exact_freshness(self):
        gid = "1217632643548483"
        override = planner.SEMANTIC_OVERRIDES[gid]
        evidence = comment((override["latest_comment_at"], "AGENTIC DESIGN REVIEW — PASS"))
        value = task(gid=gid, modified_at=override["modified_at"], comment=evidence)
        result, used = planner.apply_semantic_override(value, evidence, ("Ready", "high", "base"))
        self.assertTrue(used)
        self.assertEqual(result[0], "Needs Human Review")

        value["modified_at"] = "2026-08-19T12:00:00.000Z"
        result, used = planner.apply_semantic_override(value, evidence, ("Ready", "high", "base"))
        self.assertFalse(used)
        self.assertEqual(result[0], "Ready")

        result, used = planner.apply_semantic_override(
            task(gid=gid, modified_at=override["modified_at"], comment=evidence),
            evidence,
            ("Ready", "high", "base"),
            project_gid="1217000000000000",
        )
        self.assertFalse(used)
        self.assertEqual(result[0], "Ready")

    def test_settled_migration_overrides(self):
        expected = {
            "1217542061354795": "Done",
            "1217545391806442": "Needs Post-Merge Rollout",
            "1217591596709304": "Needs Human Review",
            "1217626783110669": "Needs Human Review",
        }
        for gid, target in expected.items():
            override = planner.SEMANTIC_OVERRIDES[gid]
            evidence = comment((override["latest_comment_at"], "current evidence"))
            value = task(gid=gid, modified_at=override["modified_at"], comment=evidence)
            result, used = planner.apply_semantic_override(value, evidence, ("Needs Processing", "low", "base"))
            self.assertTrue(used, gid)
            self.assertEqual(result[0], target, gid)

    def test_stable_title_cleanup_is_mechanical(self):
        cleaned, changed, _ = planner.stable_title("P0 — AGENT REVIEW REQUIRED — exact-byte handoff")
        self.assertTrue(changed)
        self.assertEqual(cleaned, "exact-byte handoff")


class PlannerProjectTests(unittest.TestCase):
    def test_paged_asana_lines_follows_every_cursor(self):
        pages = [
            "1 [ ] first\n# more results: --cursor next-page\n",
            "2 [x] second\n",
        ]
        with mock.patch.object(planner, "sh", side_effect=pages) as run:
            self.assertEqual(planner.paged_asana_lines(["asana", "tasks"]), ["1 [ ] first", "2 [x] second"])
        self.assertEqual(run.call_args_list[1].args[0], ["asana", "tasks", "--cursor", "next-page"])

    def test_load_project_tasks_uses_selected_project_and_hydrates_all_ids(self):
        listed = ["1217000000000001 [ ] first", "1217000000000002 [x] second"]
        with (
            mock.patch.object(planner, "paged_asana_lines", return_value=listed) as paged,
            mock.patch.object(
                planner,
                "asana_raw",
                side_effect=lambda path: {"gid": path.split("/")[2].split("?", 1)[0]},
            ),
        ):
            rows = planner.load_project_tasks("1217999999999999")
        self.assertEqual([row["gid"] for row in rows], ["1217000000000001", "1217000000000002"])
        self.assertIn("1217999999999999", paged.call_args.args[0])

    def test_default_outputs_are_project_specific(self):
        self.assertEqual(
            planner.default_output_paths("1217999999999999"),
            ("dish-asana-migration-1217999999999999.json", "dish-asana-migration-1217999999999999.csv"),
        )

    def test_build_ledger_filters_memberships_to_selected_project(self):
        selected = "1217999999999999"
        raw = {
            "gid": "1217000000000001",
            "name": "completed task",
            "completed": True,
            "completed_at": "2026-08-19T10:00:00Z",
            "created_at": "2026-08-18T10:00:00Z",
            "modified_at": "2026-08-19T10:00:00Z",
            "notes": "PRIORITY: P0",
            "memberships": [
                {"project": {"gid": selected}, "section": {"name": "Done"}},
                {"project": {"gid": "1217888888888888"}, "section": {"name": "Backlog"}},
            ],
            "custom_fields": [],
        }
        with (
            mock.patch.object(planner, "load_project_tasks", return_value=[raw]),
            mock.patch.object(planner, "hydrate_comments", return_value={raw["gid"]: None}),
            mock.patch.object(planner, "gh_pr_lineage", return_value=[]),
            mock.patch.object(planner, "controller_pr_states", return_value=({}, "test resolver")),
        ):
            plan = planner.build_ledger(selected, "Dish — Example")
        self.assertEqual(plan["project_gid"], selected)
        self.assertEqual(plan["project_name"], "Dish — Example")
        self.assertEqual(plan["tasks"][0]["current_sections"], ["Done"])
        self.assertEqual(plan["tasks"][0]["target_section"], "Done")
        self.assertFalse(plan["tasks"][0]["semantic_override_used"])

    def test_json_and_csv_cover_the_same_unique_tasks(self):
        sample = {
            "tasks": [
                {
                    "task_gid": "1217000000000001",
                    "current_name": "one",
                    "proposed_stable_title": "one",
                    "completed": False,
                    "completed_at": None,
                    "created_at": "2026-08-19T00:00:00Z",
                    "current_sections": ["Ready"],
                    "current_state_record": "READY",
                    "target_section": "Ready",
                    "classification_confidence": "high",
                    "classification_reason": "test",
                    "semantic_override_used": False,
                    "priority": "UNSET",
                    "priority_source": "test",
                    "code_areas": ["Cross-cutting / Unknown"],
                    "code_area_source": "test",
                    "version": "",
                    "owning_prs": [],
                    "handoff_sent": False,
                    "latest_handoff_at": None,
                    "handoff_age_hours": None,
                    "task_update_age_hours": 1.0,
                    "worker_started": False,
                    "worker_start_reason": "test",
                    "latest_comment_at": None,
                    "latest_comment_headline": "",
                    "modified_at": "2026-08-19T00:00:00Z",
                    "flags": [],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "plan.json"
            csv_path = Path(directory) / "plan.csv"
            planner.write_outputs(sample, json_path, csv_path)
            self.assertEqual(json.loads(json_path.read_text())["tasks"][0]["task_gid"], "1217000000000001")
            self.assertEqual(csv_path.read_text().count("1217000000000001"), 1)


if __name__ == "__main__":
    unittest.main()
