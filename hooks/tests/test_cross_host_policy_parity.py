import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PARITY = REPO_ROOT / "scripts/cross_host_policy_parity.py"
SOURCE = "dish/docs/chatgpt-projects/source.json"
MANIFEST = "dish/docs/chatgpt-projects/manifest.json"
STANDING = "dish/docs/agents/standing-invariants.json"


def _run(repo: Path):
    return subprocess.run(
        [sys.executable, str(repo / "scripts/cross_host_policy_parity.py"), "--repo-root", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )


def _copy_file(source_root: Path, target_root: Path, relative: str):
    target = target_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_root / relative, target)


def _fixture(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    target.mkdir()
    manifest = json.loads((REPO_ROOT / MANIFEST).read_text(encoding="utf-8"))
    generated = manifest["generated_role_files"]

    for relative in (
        SOURCE,
        MANIFEST,
        STANDING,
        "OPERATOR_CONTROL_PLANE.md",
        ".claude/settings.json",
        "codex/hooks.json",
        "hooks/agent-grounding",
        "scripts/agent_context.py",
        "scripts/cross_host_policy_parity.py",
    ):
        _copy_file(REPO_ROOT, target, relative)
    shutil.copytree(REPO_ROOT / "dish/docs/agents", target / "dish/docs/agents", dirs_exist_ok=True)
    for filename in generated.values():
        _copy_file(REPO_ROOT, target, f"dish/docs/chatgpt-projects/{filename}")
    return target


def _standing_extension(repo: Path):
    path = repo / STANDING
    payload = json.loads(path.read_text(encoding="utf-8"))
    entry = next(item for item in payload["invariants"] if item["id"] == "repository-context-admission")
    return path, payload, entry["delivery_extensions"]["cross_host_grounding_parity"]


def test_current_tree_has_cross_host_structural_parity():
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["repository_modifying_roles"] == [
        "development-workflow",
        "implementation",
        "integration",
        "postgresql-dark-launch",
        "workflow",
    ]
    assert payload["deferred_owner"] == "asana:task:1217547171327342"


def test_local_missing_inherited_contributor_base_fails(tmp_path):
    repo = _fixture(tmp_path)
    resolver = repo / "scripts/agent_context.py"
    text = resolver.read_text(encoding="utf-8")
    needle = "    if role in modifying:\n        startup_paths.append(CONTRIBUTOR_BASE_PATH)\n"
    assert needle in text
    resolver.write_text(text.replace(needle, "    if role in modifying:\n        pass\n", 1), encoding="utf-8")

    result = _run(repo)
    assert result.returncode == 2
    assert "lost inherited contributor-base context" in result.stdout


def test_local_missing_shared_operator_control_plane_fails(tmp_path):
    repo = _fixture(tmp_path)
    resolver = repo / "scripts/agent_context.py"
    text = resolver.read_text(encoding="utf-8")
    needle = "    startup_paths: list[str] = [source_contract, OPERATOR_CONTROL_PLANE_PATH]\n"
    assert needle in text
    resolver.write_text(text.replace(needle, "    startup_paths: list[str] = [source_contract]\n", 1), encoding="utf-8")

    result = _run(repo)
    assert result.returncode == 2
    assert "lost shared operator-control-plane context" in result.stdout


def test_chatgpt_only_removal_of_friction_delivery_fails(tmp_path):
    repo = _fixture(tmp_path)
    manifest = json.loads((repo / MANIFEST).read_text(encoding="utf-8"))
    path = repo / "dish/docs/chatgpt-projects" / manifest["generated_role_files"]["implementation"]
    text = path.read_text(encoding="utf-8")
    assert "1217443500915644" in text
    path.write_text(text.replace("1217443500915644", "REMOVED-FRICTION-IDENTITY", 1), encoding="utf-8")

    result = _run(repo)
    assert result.returncode == 2
    assert "lost friction/debt discovery delivery" in result.stdout


def test_stale_review_handoff_semantics_fail_even_when_source_and_kernel_match(tmp_path):
    repo = _fixture(tmp_path)
    source_path = repo / SOURCE
    source = json.loads(source_path.read_text(encoding="utf-8"))
    rule = next(
        item for item in source["roles"]["review"]["rules"]
        if item["id"] == "review-integration-boundary"
    )
    old = rule["text"]
    bad = "READY FOR MERGE means Review merges the candidate directly."
    rule["text"] = bad
    source_path.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")

    manifest = json.loads((repo / MANIFEST).read_text(encoding="utf-8"))
    kernel = repo / "dish/docs/chatgpt-projects" / manifest["generated_role_files"]["review"]
    kernel.write_text(kernel.read_text(encoding="utf-8").replace(old, bad), encoding="utf-8")

    result = _run(repo)
    assert result.returncode == 2
    assert "generated Review handoff semantics drift" in result.stdout


def test_explicit_host_transport_difference_remains_valid(tmp_path):
    repo = _fixture(tmp_path)
    # ChatGPT reaches the operator control plane through its generated role-index startup
    # pointer; local hosts preload the same canonical file through the resolver. Text/tool
    # shape differs intentionally, semantic source does not.
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_unaccepted_review_attempt_semantics_cannot_enter_protected_metadata(tmp_path):
    repo = _fixture(tmp_path)
    path, payload, extension = _standing_extension(repo)
    extension["protected_delivery"][0]["chatgpt_delivery"] += " with attempt_id lane A"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = _run(repo)
    assert result.returncode == 2
    assert "unaccepted Review/Assurance semantics leaked" in result.stdout


def test_review_assurance_deferral_cannot_disappear(tmp_path):
    repo = _fixture(tmp_path)
    path, payload, extension = _standing_extension(repo)
    extension["deferred"] = []
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = _run(repo)
    assert result.returncode == 2
    assert "must remain one explicit deferred dependency" in result.stdout


def test_extension_supersession_requires_durable_explicit_record(tmp_path):
    repo = _fixture(tmp_path)
    path, payload, extension = _standing_extension(repo)
    extension["status"] = "superseded"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = _run(repo)
    assert result.returncode == 2
    assert "requires durable explicit supersession" in result.stdout

    extension["supersession"] = {
        "authority_type": "marco-explicit",
        "durable_ref": "example:durable-authority",
        "decision": "retire this delivery extension",
        "effective_at": "2099-01-01T00:00:00Z",
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
