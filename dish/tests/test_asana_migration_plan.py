import datetime
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import sys
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
    def test_target_sections_match_complete_v2_lifecycle(self):
        expected = [
            "Needs Processing",
            "Needs Research",
            "Needs Agentic Review",
            "Needs Human Review",
            "Waiting on Dependency",
            "Ready",
            "Under Development",
            "Needs Post-Merge Rollout",
            "Done",
        ]
        self.assertEqual(planner.TARGET_SECTIONS, expected)

        policy = planner.__doc__.split("Target sections:\n", 1)[1].split(
            "Non-applicable reconciliation outcome:", 1
        )[0]
        documented = [line.strip() for line in policy.splitlines() if line.strip()]
        self.assertEqual(documented, planner.TARGET_SECTIONS)

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
            (
                "2026-08-19T11:00:00.000Z",
                "MARCO OVERRIDE — proceed\nGATE WAIVED BY MARCO OVERRIDE: final-stamp progression gate",
            ),
            ("2026-08-19T10:00:00.000Z", "DISPOSITION: Ready for Marco final stamp"),
        )
        value = task(
            notes="STATE: MARCO FINAL STAMP REQUIRED",
            comment=evidence,
            modified_at="2026-08-19T11:00:10.000Z",
        )
        self.assertNotEqual(self.classify(value)[0], "Needs Human Review")

    def test_older_unrelated_override_cannot_erase_current_human_decision(self):
        evidence = comment((
            "2026-08-19T10:00:00.000Z",
            "MARCO OVERRIDE — waive focused CI rerun\nGATE WAIVED BY MARCO OVERRIDE: CI gate only",
        ))
        value = task(
            notes="STATE: HUMAN DECISION PENDING — approve rollout boundary",
            comment=evidence,
            modified_at="2026-08-19T11:00:00.000Z",
        )
        self.assertEqual(self.classify(value)[0], "Needs Human Review")

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
        self.assertEqual((target, confidence), (planner.RECONCILIATION_REQUIRED, "low"))
        self.assertIn("no durable record", reason)

    def test_review_pass_plus_ready_section_is_ready(self):
        evidence = comment(("2026-08-19T10:00:00.000Z", "FOCUSED DESIGN REVIEW — PASS\nVERDICT: PASS"))
        value = task(comment=evidence, current_sections=["Ready"])
        self.assertEqual(self.classify(value)[0], "Ready")

    def test_legacy_ready_placement_alone_requires_reconciliation(self):
        value = task(notes="STATE: READY", current_sections=["Ready"])
        self.assertEqual(self.classify(value)[0], planner.RECONCILIATION_REQUIRED)

    def test_structured_marco_design_decision_in_review_headline_is_human_review(self):
        evidence = comment((
            "2026-08-19T10:00:00.000Z",
            "INDEPENDENT DESIGN RE-REVIEW — PASS / MARCO DESIGN DECISION REQUIRED",
        ))
        value = task(comment=evidence, current_sections=["Blocked / Decision"])
        self.assertEqual(self.classify(value)[0], "Needs Human Review")

    def test_plain_language_human_review_required_is_human_review(self):
        value = task(notes="STATE: HUMAN REVIEW REQUIRED — DO NOT IMPLEMENT")
        self.assertEqual(self.classify(value)[0], "Needs Human Review")

    def test_review_block_routes_to_research(self):
        evidence = comment(("2026-08-19T11:00:00.000Z", "AGENT REVIEW — BLOCK"))
        value = task(comment=evidence)
        self.assertEqual(self.classify(value)[0], "Needs Research")

    def test_incomplete_design_routes_to_research_before_design_review(self):
        value = task(notes="STATE: DESIGN REQUIRED — independent Design Review required before Implementation")
        self.assertEqual(self.classify(value)[0], "Needs Research")

    def test_generic_audit_finding_is_not_assumed_to_be_raw_intake(self):
        evidence = comment(("2026-08-19T11:00:00.000Z", "AUDIT FINDING — detailed acceptance follows"))
        value = task(comment=evidence, current_sections=["Backlog"])
        self.assertEqual(self.classify(value)[0], planner.RECONCILIATION_REQUIRED)

    def test_title_review_words_do_not_route_task(self):
        value = task(name="AGENT REVIEW REQUIRED — title-only residue")
        self.assertEqual(self.classify(value)[0], planner.RECONCILIATION_REQUIRED)

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
            "1217545391806442": "Under Development",
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

    def test_marco_reviewed_ten_task_regression_set(self):
        expected = {
            "1217632551553551": "Needs Research",
            "1217632550435438": "Needs Research",
            "1217632340344322": planner.RECONCILIATION_REQUIRED,
            "1217632337801506": "Needs Agentic Review",
            "1217614656977022": "Needs Research",
            "1217624784161458": "Needs Processing",
            "1217603621508125": planner.RECONCILIATION_REQUIRED,
            "1217560696950266": "Ready",
            "1217518869489828": "Waiting on Dependency",
            "1217516762073723": "Ready",
        }
        for gid, target in expected.items():
            override = planner.SEMANTIC_OVERRIDES[gid]
            evidence = (
                comment((override["latest_comment_at"], "current durable evidence"))
                if override["latest_comment_at"]
                else None
            )
            value = task(
                gid=gid,
                modified_at=override["modified_at"],
                comment=evidence,
                current_sections=["Backlog"],
            )
            base = planner.classify_target(value, [], {})
            result, used = planner.apply_semantic_override(value, evidence, base)
            self.assertTrue(used, gid)
            self.assertEqual(result[0], target, gid)

    def test_fixed_live_manual_chronology_regression_set(self):
        fixtures = {
            "1217513382665760": (
                task(
                    gid="1217513382665760",
                    notes="STATE: SOURCE LANDED — post-merge rollout required",
                    comment=comment(("2026-08-18T12:17:42Z", "Use task 1217591724565043 as the canonical active owner.")),
                ),
                [],
                planner.RECONCILIATION_REQUIRED,
            ),
            "1217516543178705": (
                task(
                    gid="1217516543178705",
                    comment=comment(
                        ("2026-08-17T11:05:09Z", "AGENT RE-REVIEW REQUIRED"),
                        ("2026-08-17T13:06:50Z", "ACCEPTED IMPLEMENTATION ADDENDUM"),
                        ("2026-08-17T18:22:55Z", "HANDOFF SENT"),
                    ),
                ),
                [],
                "Ready",
            ),
            "1217517324134654": (
                task(
                    gid="1217517324134654",
                    comment=comment(
                        ("2026-08-17T10:00:00Z", "HANDOFF SENT"),
                        ("2026-08-18T13:23:05Z", "MARCO BLOCK / RECOVERY HOLD\nHold this task until dependency 1217562392297322 is resolved."),
                    ),
                ),
                [],
                "Waiting on Dependency",
            ),
            "1217517555297735": (
                task(gid="1217517555297735", notes="ACCEPTANCE\n- exact bounded outcome"),
                [],
                "Needs Agentic Review",
            ),
            "1217539974328252": (
                task(gid="1217539974328252", notes="STATE: REVIEW / INTEGRATION — FOLDED INTO PR #140"),
                [{"number": 140, "state": "closed", "merged_at": "2026-08-18T10:00:00Z", "body": ""}],
                "Done",
            ),
            "1217547171327342": (
                task(gid="1217547171327342", notes="STATE: MARCO DESIGN HOLD — AUTOMATED REVIEW LIFECYCLE NOT APPROVED"),
                [],
                "Needs Human Review",
            ),
            "1217587472923725": (
                task(gid="1217587472923725", notes="STATE: AGENT DESIGN REVISION REQUIRED"),
                [],
                "Needs Research",
            ),
            "1217591715594181": (
                task(
                    gid="1217591715594181",
                    notes="STATE: IN PROGRESS — IMPLEMENTATION STARTED",
                    comment=comment(("2026-08-18T12:12:48Z", "Proceed now. No additional pre-Implementation Agent Review is required.")),
                ),
                [],
                "Ready",
            ),
            "1217591724565043": (
                task(
                    gid="1217591724565043",
                    notes="STATE: DESIGN RECHECK REQUIRED",
                    comment=comment(
                        ("2026-08-18T15:04:00Z", "INDEPENDENT AGENT DESIGN REVIEW — BLOCK"),
                        ("2026-08-18T15:15:55Z", "FOCUSED INDEPENDENT DESIGN RECHECK — PASS"),
                        ("2026-08-18T15:23:37Z", "MARCO DISPATCHED"),
                        ("2026-08-19T08:42:04Z", "POST-MERGE WORKER ROLLOUT — REQUIRED\nThis Worker setup can proceed."),
                    ),
                ),
                [{"number": 173, "state": "closed", "merged_at": "2026-08-19T07:00:00Z", "body": ""}],
                "Ready",
            ),
            "1217606745770074": (
                task(
                    gid="1217606745770074",
                    comment=comment(("2026-08-18T18:31:12Z", "IMPLEMENTATION COMPLETE / REVIEW READY — PR #175")),
                ),
                [{"number": 175, "state": "closed", "closed_at": "2026-08-18T19:00:00Z", "merged_at": None, "body": ""}],
                planner.RECONCILIATION_REQUIRED,
            ),
            "1217606746149627": (
                task(gid="1217606746149627", notes="REQUIRED OUTCOME\nA concrete bounded correction."),
                [],
                "Needs Agentic Review",
            ),
            "1217608564728454": (
                task(gid="1217608564728454", notes="STATE: IMPLEMENTATION IN PROGRESS\nACCEPTANCE\n- exact test"),
                [],
                planner.RECONCILIATION_REQUIRED,
            ),
            "1217608708597303": (
                task(gid="1217608708597303", notes="Exact implementation point for the physical gate must be resolved by the authorized Implementation task."),
                [],
                "Needs Research",
            ),
            "1217628242411152": (
                task(
                    gid="1217628242411152",
                    notes="ACCEPTANCE\n- bounded candidate",
                    current_sections=["Ready"],
                    comment=comment(("2026-08-19T12:57:41Z", "INDEPENDENT DESIGN REVIEW — PASS")),
                ),
                [],
                "Ready",
            ),
            "1217639277058985": (
                task(gid="1217639277058985", notes="STATE: TRACKING / NOT YET DESIGNED"),
                [],
                "Needs Research",
            ),
        }
        for gid, (value, prs, expected) in fixtures.items():
            self.assertEqual(self.classify(value, prs)[0], expected, gid)

    def test_later_implementation_prohibition_beats_pass_and_ready_language(self):
        value = task(
            gid="1217628696934306",
            notes="ACCEPTANCE\n- bounded candidate",
            current_sections=["Ready"],
            comment=comment(
                ("2026-08-19T10:00:00Z", "INDEPENDENT DESIGN REVIEW — PASS\nCURRENT DISPOSITION: IMPLEMENTATION READY"),
                ("2026-08-19T11:00:00Z", "No implementation, hook/config activation, or freeze relaxation is authorized by this verdict."),
            ),
        )
        self.assertEqual(self.classify(value)[0], planner.RECONCILIATION_REQUIRED)

    def test_ordinary_current_research_state_conflicting_with_older_ready_fails_closed(self):
        value = task(
            gid="1217000000000003",
            notes="STATE: RESEARCH REQUIRED",
            modified_at="2026-08-19T12:00:00Z",
            comment=comment((
                "2026-08-19T10:00:00Z",
                "INDEPENDENT DESIGN REVIEW — PASS\nCURRENT DISPOSITION: IMPLEMENTATION READY",
            )),
        )
        target, confidence, reason = self.classify(value)
        self.assertEqual((target, confidence), (planner.RECONCILIATION_REQUIRED, "low"))
        self.assertIn("notes-specific chronology is unavailable", reason)

    def test_timestamped_github_events_do_not_override_later_asana_hold(self):
        value = task(
            gid="1217000000000002",
            comment=comment(("2026-08-19T11:00:00Z", "MARCO HOLD — do not progress")),
        )
        prs = [{
            "number": 202,
            "state": "closed",
            "merged_at": "2026-08-18T11:00:00Z",
            "body": "dish-owning-task:v1 task=1217000000000002",
        }]
        events = planner.build_lifecycle_event_stream(value, prs, {})
        self.assertLess(
            next(item["sequence"] for item in events if item["kind"] == "source_merged"),
            next(item["sequence"] for item in events if item["kind"] == "hold_active"),
        )
        self.assertEqual(self.classify(value, prs)[0], "Needs Human Review")

    def test_later_current_folded_state_beats_older_ready_review(self):
        value = task(
            gid="1217509484909298",
            notes="STATE: REVIEW / INTEGRATION — FOLDED INTO PR #140",
            modified_at="2026-08-19T15:45:18Z",
            comment=comment((
                "2026-08-16T23:04:31Z",
                "INDEPENDENT AGENT RE-REVIEW — PASS\nVERDICT: PASS — IMPLEMENTATION READY.",
            )),
        )
        prs = [{
            "number": 140,
            "state": "closed",
            "merged_at": "2026-08-17T20:09:31Z",
            "body": "",
        }]
        self.assertEqual(self.classify(value, prs)[0], "Done")

    def test_unstructured_backlog_is_reconciliation_not_processing(self):
        target, confidence, _ = self.classify(task(current_sections=["Backlog"]))
        self.assertEqual((target, confidence), (planner.RECONCILIATION_REQUIRED, "low"))

    def test_fields_require_explicit_durable_evidence(self):
        notes = "Review V2 is context. Integration V1 must stabilize."
        self.assertEqual(planner.explicit_code_areas(notes)[0], [])
        self.assertEqual(planner.explicit_version(notes)[0], "")

        notes += "\nCODE AREA: Development Lifecycle / PR | CI / Tests\nVERSION: Lifecycle V4 — own generation"
        self.assertEqual(
            planner.explicit_code_areas(notes)[0],
            ["Development Lifecycle / PR", "CI / Tests"],
        )
        self.assertEqual(planner.explicit_version(notes)[0], "Lifecycle V4")

    def test_ten_task_field_regression_uses_only_explicit_records(self):
        fixtures = {
            "1217632551553551": ("Review V2 is contextual.", "UNSET", ""),
            "1217632550435438": ("", "UNSET", ""),
            "1217632340344322": ("", "UNSET", ""),
            "1217632337801506": ("Review V2 is a dependency.", "UNSET", ""),
            "1217614656977022": ("VERSION: V4", "UNSET", "V4"),
            "1217624784161458": ("", "UNSET", ""),
            "1217603621508125": ("", "UNSET", ""),
            "1217560696950266": (
                "PRIORITY: P0\nVERSION: dish-development-lifecycle:v2-pilot1",
                "P0",
                "dish-development-lifecycle:v2-pilot1",
            ),
            "1217518869489828": ("Blocked until Integration V1 stabilizes.", "UNSET", ""),
            "1217516762073723": ("PRIORITY: P2", "P2", ""),
        }
        for gid, (notes, expected_priority, expected_version) in fixtures.items():
            self.assertEqual(planner.current_priority(notes)[0], expected_priority, gid)
            self.assertEqual(planner.explicit_code_areas(notes)[0], [], gid)
            self.assertEqual(planner.explicit_version(notes)[0], expected_version, gid)

    def test_apply_and_readback_cover_section_and_all_three_fields(self):
        item = {
            "task_gid": "1217000000000001",
            "target_section": "Ready",
            "applicable": True,
            "priority": "P0",
            "code_areas": ["CI / Tests"],
            "version": "Lifecycle V4",
        }
        self.assertEqual(
            planner.build_apply_spec(item),
            {
                "task_gid": item["task_gid"],
                "section": "Ready",
                "field_updates": {
                    "Priority": "P0",
                    "Code Area": ["CI / Tests"],
                    "Version": "Lifecycle V4",
                },
            },
        )
        observed = {
            "section": "Ready",
            "priority": "P0",
            "code_areas": ["CI / Tests"],
            "version": "Lifecycle V4",
        }
        self.assertEqual(planner.readback_mismatches(item, observed), [])
        observed.update(section="Needs Processing", priority="P1", code_areas=[], version="")
        self.assertEqual(len(planner.readback_mismatches(item, observed)), 4)

    def test_reconciliation_is_not_applicable(self):
        item = {
            "task_gid": "1217000000000001",
            "target_section": planner.RECONCILIATION_REQUIRED,
            "applicable": False,
            "priority": "UNSET",
            "code_areas": [],
            "version": "",
        }
        with self.assertRaisesRegex(ValueError, "not applicable"):
            planner.build_apply_spec(item)
        with self.assertRaisesRegex(ValueError, "not applicable"):
            planner.readback_mismatches(item, {})

    def test_apply_spec_omits_unset_custom_fields(self):
        item = {
            "task_gid": "1217000000000001",
            "target_section": "Needs Research",
            "applicable": True,
            "priority": "UNSET",
            "code_areas": [],
            "version": "",
        }
        self.assertEqual(planner.build_apply_spec(item)["field_updates"], {})

    def test_stable_title_cleanup_is_mechanical(self):
        cleaned, changed, _ = planner.stable_title("P0 — AGENT REVIEW REQUIRED — exact-byte handoff")
        self.assertTrue(changed)
        self.assertEqual(cleaned, "exact-byte handoff")


class PlannerProjectTests(unittest.TestCase):
    @staticmethod
    def raw_task(project_gid="1217999999999999"):
        return {
            "gid": "1217000000000001",
            "name": "task",
            "completed": False,
            "completed_at": None,
            "created_at": "2026-08-18T10:00:00Z",
            "modified_at": "2026-08-19T10:00:00Z",
            "notes": "STATE: READY\nPRIORITY: P0",
            "memberships": [
                {"project": {"gid": project_gid}, "section": {"name": "Ready"}},
            ],
            "custom_fields": [],
        }

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
        raw = self.raw_task(selected)
        raw.update(completed=True, completed_at="2026-08-19T10:00:00Z", name="completed task")
        raw["memberships"][0]["section"]["name"] = "Done"
        raw["memberships"].append(
            {"project": {"gid": "1217888888888888"}, "section": {"name": "Backlog"}}
        )
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

    def test_comment_retrieval_failure_is_unresolved_and_invalid(self):
        selected = "1217999999999999"
        raw = self.raw_task(selected)
        failure = {"error": "temporary Asana failure", "history": []}
        with (
            mock.patch.object(planner, "load_project_tasks", return_value=[raw]),
            mock.patch.object(planner, "hydrate_comments", return_value={raw["gid"]: failure}),
            mock.patch.object(planner, "gh_pr_lineage", return_value=[]),
            mock.patch.object(planner, "controller_pr_states", return_value=({}, "test resolver")),
        ):
            plan = planner.build_ledger(selected)
        item = plan["tasks"][0]
        self.assertEqual(
            (item["target_section"], item["classification_confidence"], item["applicable"]),
            (planner.RECONCILIATION_REQUIRED, "low", False),
        )
        self.assertEqual(plan["comment_errors"][0]["task_gid"], raw["gid"])
        self.assertIn("Asana comment retrieval failed", planner.validate_plan(plan)[0])

    def test_github_lineage_failure_invalidates_plan(self):
        selected = "1217999999999999"
        raw = self.raw_task(selected)
        with (
            mock.patch.object(planner, "load_project_tasks", return_value=[raw]),
            mock.patch.object(planner, "hydrate_comments", return_value={raw["gid"]: None}),
            mock.patch.object(planner, "gh_pr_lineage", side_effect=RuntimeError("GitHub unavailable")),
            mock.patch.object(planner, "controller_pr_states", return_value=({}, "test resolver")),
        ):
            plan = planner.build_ledger(selected)
        self.assertEqual(plan["github_error"], "GitHub unavailable")
        self.assertIn("GitHub PR lineage retrieval failed", planner.validate_plan(plan)[0])

    def test_cli_exits_nonzero_for_invalid_evidence_plan(self):
        invalid = {
            "project_gid": "1217999999999999",
            "task_count": 0,
            "tasks": [],
            "github_error": "GitHub unavailable",
            "comment_errors": [],
        }
        with (
            mock.patch.object(sys, "argv", [str(SCRIPT), "--project-gid", "1217999999999999"]),
            mock.patch.object(planner, "build_ledger", return_value=invalid),
            mock.patch.object(planner, "write_outputs"),
            mock.patch.object(planner, "summarize"),
            self.assertRaises(SystemExit) as raised,
        ):
            planner.main()
        self.assertEqual(raised.exception.code, 2)

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
                    "applicable": True,
                    "classification_confidence": "high",
                    "classification_reason": "test",
                    "semantic_override_used": False,
                    "priority": "UNSET",
                    "priority_source": "test",
                    "code_areas": ["Cross-cutting / Unknown"],
                    "code_area_source": "test",
                    "version": "",
                    "version_source": "test",
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
