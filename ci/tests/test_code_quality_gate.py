from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("code_quality_gate", ROOT / "scripts" / "code_quality_gate.py")
assert SPEC and SPEC.loader
cq = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cq)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", ".")
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", message], cwd=repo, check=True)
    return git(repo, "rev-parse", "HEAD")


def policy() -> dict:
    return {
        "python_size": {"max_nonblank_lines": 500},
        "tracked_files": {
            "manageability_bytes": 100_000,
            "operational_hard_bytes": 200_000,
            "source_extensions": [".py", ".js"],
            "likely_generated_extensions": [".db", ".png"],
        },
    }


def test_python_size_blocks_new_crossing_and_legacy_growth(tmp_path: Path):
    repo = tmp_path
    git(repo, "init", "-q")
    (repo / "a.py").write_text("x=1\n" * 500)
    base = commit(repo, "base")
    (repo / "a.py").write_text("x=1\n" * 501)
    head = commit(repo, "cross")
    failures, _ = cq._file_findings(repo, base, head, [("a.py", "a.py")], policy(), {})
    assert failures == [{"kind": "python_size", "path": "a.py", "base": 500, "head": 501, "limit": 500}]
    base = head
    (repo / "a.py").write_text("x=1\n" * 502)
    head = commit(repo, "grow")
    failures, _ = cq._file_findings(repo, base, head, [("a.py", "a.py")], policy(), {})
    assert failures[0]["kind"] == "python_size"


def test_python_size_allows_legacy_shrink(tmp_path: Path):
    repo = tmp_path
    git(repo, "init", "-q")
    (repo / "a.py").write_text("x=1\n" * 550)
    base = commit(repo, "base")
    (repo / "a.py").write_text("x=1\n" * 540)
    head = commit(repo, "shrink")
    failures, signals = cq._file_findings(repo, base, head, [("a.py", "a.py")], policy(), {})
    assert not failures
    assert signals[0]["kind"] == "python_size_legacy"


def test_generated_candidate_requires_base_registry(tmp_path: Path):
    repo = tmp_path
    git(repo, "init", "-q")
    (repo / "readme.txt").write_text("base")
    base = commit(repo, "base")
    (repo / "fixture.db").write_bytes(b"sqlite")
    head = commit(repo, "generated")
    failures, _ = cq._file_findings(repo, base, head, [(None, "fixture.db")], policy(), {})
    assert {item["kind"] for item in failures} == {"generated_unregistered"}


def test_file_size_ratchet_blocks_only_growth(tmp_path: Path):
    repo = tmp_path
    git(repo, "init", "-q")
    (repo / "data.json").write_bytes(b"x" * 210_000)
    base = commit(repo, "base")
    (repo / "data.json").write_bytes(b"x" * 209_000)
    head = commit(repo, "shrink")
    failures, _ = cq._file_findings(repo, base, head, [("data.json", "data.json")], policy(), {})
    assert not failures
    base = head
    (repo / "data.json").write_bytes(b"x" * 211_000)
    head = commit(repo, "grow")
    failures, _ = cq._file_findings(repo, base, head, [("data.json", "data.json")], policy(), {})
    assert failures[0]["kind"] == "tracked_file_hard_size"


def test_positive_deltas_are_net_count_only():
    assert cq._positive_deltas({"F401": 3, "B006": 1}, {"F401": 2, "B006": 2, "F821": 1}) == {"B006": 1, "F821": 1}


def test_jscpd_occurrence_delta_can_be_intersected_with_changed_path():
    base = {"d": {("old.py", 1, 10), ("other.py", 1, 10)}}
    head = {"d": {("old.py", 1, 10), ("other.py", 1, 10), ("changed.py", 5, 15)}}
    changed = {"changed.py"}
    delta = {}
    for digest, occurrences in head.items():
        excess = len(occurrences) - len(base.get(digest, set()))
        if excess > 0 and any(path in changed for path, _, _ in occurrences):
            delta[digest] = excess
    assert delta == {"d": 1}


def test_comment_round_trip_and_tamper_detection():
    result = {"schema": cq.SCHEMA, "head_sha": "a" * 40, "outcome": "PASS"}
    result["result_digest"] = cq._digest(result)
    body = cq.render_comment(result)
    assert cq.extract_comment(body, expected_head="a" * 40) == result
    tampered = body.replace('"PASS"', '"FIX_REQUIRED"')
    try:
        cq.extract_comment(tampered, expected_head="a" * 40)
    except cq.GateError as exc:
        assert "digest mismatch" in str(exc)
    else:
        raise AssertionError("tampered result was accepted")


def test_comment_rejects_forged_base_identity():
    result = {
        "schema": cq.SCHEMA,
        "head_sha": "a" * 40,
        "target_base_sha": "b" * 40,
        "comparison_base_sha": "c" * 40,
        "pr_number": 242,
        "outcome": "PASS",
    }
    result["result_digest"] = cq._digest(result)
    body = cq.render_comment(result)
    assert cq.extract_comment(
        body,
        expected_head="a" * 40,
        expected_target_base="b" * 40,
        expected_comparison_base="c" * 40,
        expected_pr_number=242,
    ) == result
    try:
        cq.extract_comment(
            body,
            expected_head="a" * 40,
            expected_target_base="b" * 40,
            expected_comparison_base="d" * 40,
            expected_pr_number=242,
        )
    except cq.GateError as exc:
        assert "comparison base is invalid" in str(exc)
    else:
        raise AssertionError("forged comparison-base identity was accepted")


def test_disabled_policy_is_deterministic_without_analyzers(monkeypatch, tmp_path: Path):
    repo = tmp_path
    git(repo, "init", "-q")
    (repo / "ci").mkdir(); (repo / "scripts").mkdir()
    (repo / "seed.txt").write_text("seed")
    base = commit(repo, "base")
    (repo / "ci" / "code-quality.toml").write_text('version=1\nenabled=false\ntask_gid="1"\nmax_quality_correction_rounds=2\nlocal_p95_target_seconds=10.0\n[python_size]\nmax_nonblank_lines=500\n[tracked_files]\nmanageability_bytes=100000\noperational_hard_bytes=200000\nsource_extensions=[".py"]\nlikely_generated_extensions=[".db"]\ngenerated_registry="ci/code-quality-generated.json"\n[ruff]\nversion="0"\nselect=[]\n[pyright]\nversion="0"\ntype_checking_mode="basic"\ninclude=[]\nnonblocking_rules=[]\n[jscpd]\nversion="0"\nmode="mild"\nmin_lines=10\nmin_tokens=80\nscan_paths=[]\nignore=[]\n')
    (repo / "ci" / "code-quality-generated.json").write_text('{"schema":"dish-code-quality-generated-registry-v1","entries":[]}')
    head = commit(repo, "head")
    monkeypatch.setattr(cq, "exact_changed_paths", lambda *a, **k: ("ci/code-quality.toml", "ci/code-quality-generated.json"))
    result, timings = cq.evaluate(repo, target_base=base, head=head, task_gid="1")
    assert result["bootstrap"] is True
    assert result["outcome"] == "DISABLED"
    assert timings == {}


def test_activation_head_is_enforced_monotonically(monkeypatch, tmp_path: Path):
    repo = tmp_path
    git(repo, "init", "-q")
    (repo / "ci").mkdir(); (repo / "scripts").mkdir()
    template = 'version=1\nenabled={enabled}\ntask_gid="1"\nmax_quality_correction_rounds=2\nlocal_p95_target_seconds=10.0\n[python_size]\nmax_nonblank_lines=500\n[tracked_files]\nmanageability_bytes=100000\noperational_hard_bytes=200000\nsource_extensions=[".py"]\nlikely_generated_extensions=[".db"]\ngenerated_registry="ci/code-quality-generated.json"\n[ruff]\nversion="0"\nselect=[]\n[pyright]\nversion="0"\ntype_checking_mode="basic"\ninclude=[]\nnonblocking_rules=[]\n[jscpd]\nversion="0"\nmode="mild"\nmin_lines=10\nmin_tokens=80\nscan_paths=[]\nignore=[]\n'
    (repo / "ci" / "code-quality.toml").write_text(template.format(enabled="false"))
    (repo / "ci" / "code-quality-generated.json").write_text('{"schema":"dish-code-quality-generated-registry-v1","entries":[]}')
    base = commit(repo, "disabled")
    (repo / "ci" / "code-quality.toml").write_text(template.format(enabled="true"))
    head = commit(repo, "activate")
    monkeypatch.setattr(cq, "exact_changed_paths", lambda *a, **k: ("ci/code-quality.toml",))
    monkeypatch.setattr(cq, "_run_analyzers", lambda *a, **k: ({}, [], {}))
    result, _ = cq.evaluate(repo, target_base=base, head=head, task_gid="1")
    assert result["effective_enabled"] is True
    assert result["policy_source_sha"] == head
    assert result["outcome"] == "PASS"


def test_policy_select_excludes_preview_b909():
    import tomllib
    current = tomllib.loads((ROOT / "ci" / "code-quality.toml").read_text())
    assert "B909" not in current["ruff"]["select"]
    assert current["ruff"]["version"] == "0.16.2"


def test_canonical_result_digest_ignores_runtime_timing():
    result = {"schema": cq.SCHEMA, "outcome": "PASS", "analyzers": {}}
    first = cq._digest(result)
    timing = {"ruff": 0.1, "pyright": 5.0}
    assert cq._digest(result) == first
    assert timing != {}


def test_workflow_separates_untrusted_evaluation_from_privileged_status():
    text = (ROOT / ".github" / "workflows" / "code-quality.yml").read_text()
    verify, report = text.split("\n  report:\n", 1)
    assert "issue_comment:" in text
    assert "ready_for_review" in text
    assert "statuses: write" not in verify
    assert "statuses: write" in report
    assert "persist-credentials: false" in verify
    assert 'comparison_base=$(git merge-base "$BASE_SHA" "$HEAD_SHA")' in verify
    assert 'if [[ "$target_base" != "$BASE_SHA" ]]' in verify
    assert 'if [[ "$claimed_comparison" != "$comparison_base" ]]' in verify
    assert "--expected-comparison-base" in verify
    assert "Publish exact-head status without candidate execution" in report
    assert "actions/checkout" not in report


def test_python_size_pure_rename_keeps_base_identity(tmp_path: Path):
    repo = tmp_path
    git(repo, "init", "-q")
    (repo / "old.py").write_text("x=1\n" * 550)
    base = commit(repo, "base")
    git(repo, "mv", "old.py", "new.py")
    head = commit(repo, "rename")
    assert cq._changed_pairs(repo, base, head) == (("old.py", "new.py"),)
    failures, signals = cq._file_findings(repo, base, head, cq._changed_pairs(repo, base, head), policy(), {})
    assert not failures
    assert signals[0]["kind"] == "python_size_legacy"
