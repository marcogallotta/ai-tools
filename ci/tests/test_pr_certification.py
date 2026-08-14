from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pr_certification.py"
SPEC = importlib.util.spec_from_file_location("pr_certification", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

CANDIDATE = "a" * 40
BASE = "b" * 40


def event(*, body: str, candidate: str = CANDIDATE, head: str = CANDIDATE):
    return {
        "action": "submitted",
        "review": {
            "id": 88,
            "state": "commented",
            "commit_id": candidate,
            "submitted_at": "2026-08-14T08:00:00Z",
            "body": body,
        },
        "pull_request": {
            "number": 31,
            "head": {"sha": head},
            "base": {"sha": BASE},
        },
    }


def plan(*, groups: list[str], lanes: list[str], dish_selector=None, force_full=False, repo=False):
    classifications = []
    if repo:
        classifications.append({"path": "scripts/example.py", "scope": "repository", "classification": "root-scripts"})
    return {
        "identity": {"candidate_sha": CANDIDATE, "base_sha": BASE, "merge_base_sha": "c" * 40},
        "classifications": classifications,
        "dish_selector": dish_selector or {"focused_tests": [], "lanes": []},
        "selected_groups": groups,
        "selected_lanes": lanes,
        "force_full": force_full,
    }


def test_formal_review_commit_id_is_candidate_and_review_can_only_add_lanes():
    identity = module.review_event_identity(event(body="VERDICT: MERGE\nCERTIFICATION ADD LANES: browser acceptance; frontend static"))
    assert identity is not None
    assert identity["candidate_sha"] == CANDIDATE
    assert identity["semantic_additions"] == ("browser acceptance", "frontend static")
    assert module.review_event_identity(event(body="VERDICT: BLOCK\nCERTIFICATION ADD LANES: browser acceptance")) is None
    with pytest.raises(module.PRCertificationError, match="stale"):
        module.review_event_identity(event(body="VERDICT: MERGE", head="d" * 40))


def test_review_additions_have_no_removal_language_or_negative_operation():
    assert module.review_additional_lanes("VERDICT: MERGE\nCERTIFICATION ADD LANES: NONE") == ()
    assert module.review_additional_lanes("VERDICT: MERGE") == ()
    # A removal-looking token is merely an unknown additive lane and is rejected by planner policy;
    # the adapter has no subtraction/removal operation.
    assert module.review_additional_lanes("CERTIFICATION ADD LANES: -native PostgreSQL certification") == (
        "-native PostgreSQL certification",
    )


def test_exact_changed_paths_include_both_sides_of_rename(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "old.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "old.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(tmp_path), "mv", "old.txt", "new.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "rename"], check=True)
    candidate = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    assert module.exact_changed_paths(tmp_path, merge_base=base, candidate=candidate) == ("new.txt", "old.txt")


@pytest.mark.parametrize(
    ("groups", "lanes", "expected"),
    [
        (["python-control-plane"], ["repository control-plane"], {"python-control-plane"}),
        (["frontend-static"], ["frontend static"], {"frontend-static"}),
        (["native-postgresql"], ["native PostgreSQL certification"], {"native-postgresql"}),
        (["browser-acceptance"], ["browser acceptance"], {"browser-acceptance"}),
    ],
)
def test_execution_spec_contains_only_planner_selected_groups(groups, lanes, expected):
    spec = module.build_execution_spec(plan(groups=groups, lanes=lanes, repo=True), plan_digest="f" * 64)
    assert set(spec["required_groups"]) == expected


def test_native_postgresql_execution_spec_uses_structured_bounded_waivers():
    spec = module.build_execution_spec(
        plan(groups=["native-postgresql"], lanes=["native PostgreSQL certification"]),
        plan_digest="f" * 64,
    )
    argv = spec["required_groups"]["native-postgresql"][0]["argv"]
    values = [argv[index + 1] for index, value in enumerate(argv) if value == "--waive-skip"]
    assert len(values) == 4
    records = [json.loads(value) for value in values]
    assert all(set(record) == {
        "nodeid",
        "expected_reason_sha256",
        "owner_task_gid",
        "review_by",
        "justification",
    } for record in records)
    assert {record["owner_task_gid"] for record in records} == {"1217428310522281"}
    assert {record["review_by"] for record in records} == {"2026-09-07"}
    assert all(len(record["expected_reason_sha256"]) == 64 for record in records)


def test_full_plan_materializes_all_four_groups_and_unselected_groups_are_absent():
    spec = module.build_execution_spec(
        plan(
            groups=list(module.certification_plan.EXPECTED_GROUPS),
            lanes=[
                "repository control-plane",
                "ordinary full suite",
                "frontend static",
                "native PostgreSQL certification",
                "browser acceptance",
            ],
            force_full=True,
            repo=True,
        ),
        plan_digest="f" * 64,
    )
    assert list(spec["required_groups"]) == list(module.certification_plan.EXPECTED_GROUPS)

    frontend = module.build_execution_spec(
        plan(groups=["frontend-static"], lanes=["frontend static"]), plan_digest="e" * 64
    )
    assert set(frontend["required_groups"]) == {"frontend-static"}


def test_dish_focused_python_commands_remain_selector_scoped():
    spec = module.build_execution_spec(
        plan(
            groups=["python-control-plane"],
            lanes=[],
            dish_selector={
                "focused_tests": [
                    "tests/test_application_service.py",
                    "tests/postgresql/native/test_locking.py",
                    "frontend/tests/test_frontend.py",
                ],
                "lanes": [],
            },
        ),
        plan_digest="d" * 64,
    )
    command = spec["required_groups"]["python-control-plane"][0]
    assert command["cwd"] == "dish"
    assert "tests/test_application_service.py" in command["argv"]
    assert "tests/postgresql/native/test_locking.py" not in command["argv"]
    assert "frontend/tests/test_frontend.py" not in command["argv"]
