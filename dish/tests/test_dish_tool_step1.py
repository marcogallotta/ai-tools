import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "dish-version-current"
GIT_COMMIT = BIN_DIR.parent / "tools" / "git-commit"
sys.path.insert(0, str(BIN_DIR))

from dish_tool import cli as dish_cli  # noqa: E402
from dish_tool.commands import DishApplication  # noqa: E402
from dish_tool.errors import ReleaseResolutionError  # noqa: E402
from dish_tool.releases import (  # noqa: E402
    configured_honest_path,
    current_verification_protocol_release,
    parse_dish_version,
    resolve_release,
    resolve_verification_protocol,
)




def test_role_aware_release_loader_receives_requested_role():
    calls = []
    expected = object()

    def loader(role):
        calls.append(role)
        return expected

    app = DishApplication(None, None, release_loader=loader)

    assert app._load_release("research") is expected
    assert calls == ["research"]


def test_cli_preserves_startup_compatibility_error(monkeypatch, capsys):
    def fail_startup():
        raise ReleaseResolutionError(
            "honest_path_unconfigured",
            "DISH_HONEST_PATH must name the Honest rollout checkout",
        )

    monkeypatch.setattr(dish_cli, "build_application", fail_startup)

    status = dish_cli.main(["read", "123", "--agent", "gpt"])
    payload = json.loads(capsys.readouterr().out)

    assert status == 2
    assert payload["code"] == "VALIDATION_FAILED"
    assert payload["retryable"] is False
    assert payload["errors"] == [{"rule": "honest_path_unconfigured"}]

def copy_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "honest-rollout"
    shutil.copytree(FIXTURE_DIR, root)
    return root


def run_git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def init_git(repo: Path) -> str:
    run_git(repo, "init", "-b", "main")
    run_git(repo, "config", "user.name", "Fixture")
    run_git(repo, "config", "user.email", "fixture@example.invalid")
    files = [str(path.relative_to(repo)) for path in repo.rglob("*") if path.is_file()]
    run_git(repo, "add", *files)
    run_git(repo, "commit", "-m", "initial compatible baseline")
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()


def test_valid_current_pair_loads_schema_requested_protocol_and_migrations(tmp_path):
    root = copy_fixture(tmp_path)

    release = resolve_release(
        root, protocol_role="research", include_migrations=True
    )

    assert release.protocol_version == "1.0.8"
    assert release.schema_version == "2"
    assert set(release.protocols) == {"research"}
    assert release.schema["schema_kind"] == "dish-task"
    assert set(release.migration_metadata) == {
        "dish-schema-migrations/0002-canonical-document.json"
    }
    assert all(rule["id"] and rule["source"] for rule in release.schema["rules"])


@pytest.mark.parametrize("role", ["planning", "research", "verification", "cooking"])
def test_only_requested_stage_protocol_is_loaded(tmp_path, role):
    root = copy_fixture(tmp_path)
    for other, filename in {
        "planning": "dish-planning-protocol.md",
        "research": "dish-research-protocol.md",
        "verification": "dish-verification-protocol.md",
        "cooking": "dish-cooking-protocol.md",
    }.items():
        if other != role:
            (root / filename).unlink()

    release = resolve_release(root, protocol_role=role)

    assert set(release.protocols) == {role}


def test_migration_metadata_is_loaded_only_when_requested(tmp_path):
    root = copy_fixture(tmp_path)
    (root / "dish-schema-migrations" / "0002-canonical-document.json").unlink()

    assert resolve_release(root).migration_metadata == {}
    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_release(root, include_migrations=True)
    assert exc.value.rule == "migration_missing"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("PROTOCOL_VERSION=1.0.0\n", "dish_version_missing_key"),
        (
            "PROTOCOL_VERSION=1.0.0\nPROTOCOL_VERSION=2.0.0\nSCHEMA_VERSION=1\n",
            "dish_version_duplicate_key",
        ),
        (
            "PROTOCOL_VERSION=1.0.0\nSCHEMA_VERSION=1\nOTHER=x\n",
            "dish_version_unknown_key",
        ),
        (
            "PROTOCOL_VERSION 1.0.0\nSCHEMA_VERSION=1\n",
            "dish_version_malformed",
        ),
        (
            "PROTOCOL_VERSION=\nSCHEMA_VERSION=1\n",
            "dish_version_empty_value",
        ),
    ],
)
def test_dish_version_parser_fails_deterministically(text, rule):
    with pytest.raises(ReleaseResolutionError) as exc:
        parse_dish_version(text)
    assert exc.value.rule == rule


def test_unconfigured_honest_path_has_no_default():
    with pytest.raises(ReleaseResolutionError) as exc:
        configured_honest_path({})
    assert exc.value.rule == "honest_path_unconfigured"


def test_configured_production_style_path_without_dish_version_fails_distinctly(tmp_path):
    root = tmp_path / "honest-production"
    root.mkdir()
    (root / "dish-planning-protocol.md").write_text("legacy\n")

    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_release(root)
    assert exc.value.rule == "dish_version_missing"


@pytest.mark.parametrize(
    ("key", "value", "rule"),
    [
        ("PROTOCOL_VERSION", "9.0.0", "protocol_version_unsupported"),
        ("SCHEMA_VERSION", "99", "schema_version_unsupported"),
    ],
)
def test_unsupported_declared_version_fails_closed(tmp_path, key, value, rule):
    root = copy_fixture(tmp_path)
    values = parse_dish_version((root / "DISH_VERSION").read_text())
    values[key] = value
    (root / "DISH_VERSION").write_text(
        f"PROTOCOL_VERSION={values['PROTOCOL_VERSION']}\n"
        f"SCHEMA_VERSION={values['SCHEMA_VERSION']}\n"
    )

    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_release(root)
    assert exc.value.rule == rule


def test_schema_declared_version_mismatch_fails_closed(tmp_path):
    root = copy_fixture(tmp_path)
    schema_path = root / "dish-task-schema.json"
    schema = json.loads(schema_path.read_text())
    schema["schema_version"] = "3"
    schema_path.write_text(json.dumps(schema))

    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_release(root)
    assert exc.value.rule == "schema_version_mismatch"


@pytest.mark.parametrize(
    ("change", "rule"),
    [("missing", "schema_missing"), ("malformed", "schema_malformed")],
)
def test_missing_or_malformed_schema_fails_closed(tmp_path, change, rule):
    root = copy_fixture(tmp_path)
    path = root / "dish-task-schema.json"
    if change == "missing":
        path.unlink()
    else:
        path.write_text("{not-json\n")

    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_release(root)
    assert exc.value.rule == rule


def test_submission_bundle_is_role_specific_not_task_lifetime_bundle(tmp_path):
    root = copy_fixture(tmp_path)
    planning = resolve_release(root, protocol_role="planning")
    research = resolve_release(root, protocol_role="research")

    assert set(planning.bundle_for_submission("planning")) == {"planning"}
    assert set(research.bundle_for_submission("initial")) == {"research"}
    assert "verification" not in research.bundle_for_submission("initial")


def test_historical_verification_git_text_ignores_current_compatibility_gate(tmp_path):
    root = copy_fixture(tmp_path)
    old_text = (root / "dish-verification-protocol.md").read_text()
    commit = init_git(root)
    (root / "DISH_VERSION").write_text(
        "PROTOCOL_VERSION=unsupported-current\nSCHEMA_VERSION=999\n"
    )
    (root / "dish-verification-protocol.md").write_text("new incompatible text\n")

    snapshot = resolve_verification_protocol(root, commit)

    assert snapshot.source == "git"
    assert snapshot.text == old_text
    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_release(root)
    assert exc.value.rule == "protocol_version_unsupported"


def test_unreachable_historical_verification_commit_has_distinct_error(tmp_path):
    root = copy_fixture(tmp_path)
    init_git(root)

    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_verification_protocol(root, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    assert exc.value.rule == "verification_release_unreachable"


def test_non_git_verification_hash_round_trips_and_detects_changed_text(tmp_path):
    root = copy_fixture(tmp_path)
    current = current_verification_protocol_release(
        root, read_at=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    )

    assert current.identity.startswith("sha256:")
    assert "; read-at=2026-07-25T12:00:00Z" in current.identity
    recovered = resolve_verification_protocol(root, current.identity)
    assert recovered.text == current.text

    (root / "dish-verification-protocol.md").write_text("changed\n")
    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_verification_protocol(root, current.identity)
    assert exc.value.rule == "verification_release_unreachable"


def run_commit_helper(repo: Path, *paths: str):
    return subprocess.run(
        [str(GIT_COMMIT), "-C", str(repo), *paths, "-m", "test change"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_git_commit_blocks_protocol_change_without_protocol_bump(tmp_path):
    root = copy_fixture(tmp_path)
    init_git(root)
    (root / "dish-research-protocol.md").write_text("changed\n")

    completed = run_commit_helper(root, "dish-research-protocol.md")

    assert completed.returncode != 0
    assert "PROTOCOL_VERSION bump" in completed.stderr


def test_git_commit_blocks_schema_change_without_both_bumps(tmp_path):
    root = copy_fixture(tmp_path)
    init_git(root)
    schema_path = root / "dish-task-schema.json"
    schema_path.write_text(schema_path.read_text() + "\n")
    (root / "DISH_VERSION").write_text(
        "PROTOCOL_VERSION=1.0.3\nSCHEMA_VERSION=2\n"
    )

    completed = run_commit_helper(root, "dish-task-schema.json", "DISH_VERSION")

    assert completed.returncode != 0
    assert "both PROTOCOL_VERSION and SCHEMA_VERSION bumps" in completed.stderr


def test_git_commit_allows_governed_change_with_required_bumps(tmp_path):
    root = copy_fixture(tmp_path)
    init_git(root)
    (root / "dish-research-protocol.md").write_text("changed\n")
    (root / "DISH_VERSION").write_text(
        "PROTOCOL_VERSION=1.0.3\nSCHEMA_VERSION=2\n"
    )

    completed = run_commit_helper(root, "dish-research-protocol.md", "DISH_VERSION")

    assert completed.returncode == 0, completed.stderr


def test_git_commit_allows_ordinary_code_only_change_without_version_bump(tmp_path):
    root = copy_fixture(tmp_path)
    (root / "helper.py").write_text("print('one')\n")
    init_git(root)
    (root / "helper.py").write_text("print('two')\n")

    completed = run_commit_helper(root, "helper.py")

    assert completed.returncode == 0, completed.stderr
