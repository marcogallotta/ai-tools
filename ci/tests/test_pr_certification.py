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
            "id": 88, "state": "commented", "commit_id": candidate,
            "submitted_at": "2026-08-14T08:00:00Z", "body": body,
        },
        "pull_request": {
            "number": 31, "head": {"sha": head}, "base": {"sha": BASE},
        },
    }


def target(
    target_id: str, runner: str, selector: str, boundary: str,
    requirements: list[str],
) -> dict[str, object]:
    return {
        "id": target_id, "runner": runner, "selector": selector,
        "execution_boundary": boundary, "requirements": requirements,
    }


def plan(targets: list[dict[str, object]]):
    return {
        "identity": {
            "candidate_sha": CANDIDATE, "base_sha": BASE, "merge_base_sha": "c" * 40,
        },
        "selected_targets": targets,
    }


def test_formal_review_commit_id_is_candidate_and_review_only_adds_lanes():
    identity = module.review_event_identity(event(
        body="VERDICT: MERGE\nCERTIFICATION ADD LANES: browser acceptance; frontend static"
    ))
    assert identity is not None
    assert identity["candidate_sha"] == CANDIDATE
    assert identity["semantic_additions"] == ("browser acceptance", "frontend static")
    assert module.review_event_identity(event(body="VERDICT: BLOCK")) is None
    with pytest.raises(module.PRCertificationError, match="stale"):
        module.review_event_identity(event(body="VERDICT: MERGE", head="d" * 40))


def test_review_additions_have_no_subtraction_operation():
    assert module.review_additional_lanes("VERDICT: MERGE\nCERTIFICATION ADD LANES: NONE") == ()
    assert module.review_additional_lanes("VERDICT: MERGE") == ()
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


def test_execution_spec_preserves_target_identity_and_runtime_requirements():
    spec = module.build_execution_spec(plan([
        target("repo", "repo-pytest", "ci/tests/test_pr_gate.py", "python-control-plane", ["python"]),
        target("frontend", "frontend-static", "frontend-static", "frontend-static", ["node"]),
    ]), plan_digest="f" * 64)
    assert spec["schema"] == "dish-certification-execution-spec-v2"
    assert [item["id"] for item in spec["targets"]] == ["repo", "frontend"]
    assert spec["targets"][0]["requirements"] == ["python"]
    assert spec["targets"][0]["commands"][0]["argv"][-1] == "ci/tests/test_pr_gate.py"


def test_native_target_preserves_focused_selector_and_matching_waiver():
    selected = "tests/postgresql/native/test_process_failure_command.py"
    spec = module.build_execution_spec(plan([
        target("native", "native-postgresql", selected, "native-postgresql", ["python", "postgresql"])
    ]), plan_digest="a" * 64)
    argv = spec["targets"][0]["commands"][0]["argv"]
    assert argv[argv.index("--test-file") + 1] == selected
    assert argv[argv.index("--expected-head") + 1] == CANDIDATE
    waivers = [json.loads(argv[index + 1]) for index, value in enumerate(argv) if value == "--waive-skip"]
    assert len(waivers) == 1
    assert waivers[0]["nodeid"].startswith(selected + "::")


def test_full_native_fallback_keeps_structured_bounded_waivers():
    spec = module.build_execution_spec(plan([
        target("native-full", "native-postgresql", "full", "native-postgresql", ["python", "postgresql"])
    ]), plan_digest="b" * 64)
    argv = spec["targets"][0]["commands"][0]["argv"]
    records = [json.loads(argv[index + 1]) for index, value in enumerate(argv) if value == "--waive-skip"]
    assert len(records) == 4
    assert all(set(record) == {
        "nodeid", "expected_reason_sha256", "owner_task_gid", "review_by", "justification",
    } for record in records)


def test_python_boundary_fallback_is_explicit_three_surface_execution():
    spec = module.build_execution_spec(plan([
        target("python-full", "repo-python-full", "repository-python", "python-control-plane", ["python", "node"])
    ]), plan_digest="c" * 64)
    commands = spec["targets"][0]["commands"]
    assert len(commands) == 3
    assert commands[0]["argv"][-1] == "ci/tests"
    assert commands[1]["argv"][-1] == "tools/tests"
    assert commands[2]["cwd"] == "dish"
    assert commands[2]["argv"][-1] == "tests"


def test_base_presence_is_exact_for_added_and_deleted_paths():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    present = module._paths_present(ROOT, head, ["README.md", "does-not-exist"])
    assert present == ("README.md",)


def test_unknown_runner_is_rejected():
    with pytest.raises(module.PRCertificationError, match="unsupported runner"):
        module.build_execution_spec(plan([
            target("bad", "shell", "echo nope", "python-control-plane", [])
        ]), plan_digest="d" * 64)


def test_base_graph_evidence_returns_the_base_arbiter_union(monkeypatch, tmp_path: Path):
    import io
    import tarfile
    from types import SimpleNamespace

    changed = ("scripts/test_impact_arbiter.py",)
    base_envelope = {
        "format": "dish-test-obligations-v1",
        "provenance": "base",
        "engine_identity": "b" * 64,
        "changed_paths": list(changed),
        "obligations": [],
    }
    candidate_envelope = {
        "format": "dish-test-obligations-v1",
        "provenance": "candidate",
        "engine_identity": "c" * 64,
        "changed_paths": list(changed),
        "obligations": [],
    }
    base_union = {
        "format": "dish-test-obligation-union-v1",
        "base_engine_identity": "b" * 64,
        "candidate_engine_identity": "c" * 64,
        "base_obligation_digest": "1" * 64,
        "candidate_obligation_digest": "2" * 64,
        "union_digest": "3" * 64,
        "semantic_keys": [],
        "obligations": [],
    }
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        for name in ("scripts/test_impact_graph.py", "scripts/test_impact_arbiter.py"):
            payload = b"# placeholder for BASE materialization test\n"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    calls = 0
    def fake_run(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(returncode=0, stdout=archive_bytes.getvalue(), stderr=b"")
        if calls == 2:
            return SimpleNamespace(returncode=0, stdout=json.dumps(base_envelope), stderr="")
        if calls == 3:
            return SimpleNamespace(returncode=0, stdout=json.dumps(base_union), stderr="")
        raise AssertionError(f"unexpected subprocess call: {argv}")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    observed_base, observed_union, compatible = module._base_graph_evidence(
        tmp_path,
        merge_base="d" * 40,
        paths=changed,
        candidate_envelope=candidate_envelope,
    )
    assert compatible is True
    assert observed_base == base_envelope
    assert observed_union == base_union


def test_selector_gap_evidence_binds_exact_pr_head_review_and_run():
    payload = {
        "selector_gaps": [{
            "gap_id": "f" * 64,
            "path": "scripts/example.py",
        }],
    }
    identity = {"pr_number": 31, "review_id": 88}
    module.bind_selector_gap_evidence(
        payload, identity=identity, candidate=CANDIDATE,
        run_id="12345", run_attempt="2",
    )
    assert payload["selector_gaps"][0]["evidence"] == {
        "pr_number": 31,
        "head_sha": CANDIDATE,
        "review_id": 88,
        "run_id": "12345",
        "run_attempt": "2",
    }
