import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dish_tool.errors import ReleaseResolutionError
from dish_tool.releases import resolve_release


FIXTURE_RELEASE_DIR = Path(__file__).resolve().parent / "fixtures" / "dish-version-current"

PROTOCOLS = {
    "dish-planning-protocol.md": "# Planning protocol\nUse the planning manifest.\n",
    "dish-research-protocol.md": "# Research protocol\nBuild the complete task.\n",
    "dish-verification-protocol.md": "# Verification protocol\nVerify the complete task.\n",
}


def planning_manifest(version):
    return {
        "protocol_release": version,
        "manifest_kind": "planning",
        "headings": {
            "required": ["# PLANNING BRIEF"],
            "optional": [],
            "exactly_once": ["# PLANNING BRIEF"],
            "allowed": ["# PLANNING BRIEF"],
        },
        "labels": {
            "required": ["Destination section", "Exemptions"],
            "optional": ["Research notes"],
            "exactly_once": ["Destination section", "Exemptions"],
            "allowed": ["Destination section", "Exemptions", "Research notes"],
        },
        "contextual_labels": [],
        "exemptions": {
            "label": "Exemptions",
            "none_value": "None",
            "allowed_tags": [
                "nutrition-kcal",
                "nutrition-protein",
                "nutrition-fat",
            ],
        },
        "destination_section": {
            "label": "Destination section",
            "pattern": r"^(?P<name>[^()]+?)\s*\((?P<gid>[0-9]+)\)$",
        },
    }


def complete_manifest(version):
    return {
        "protocol_release": version,
        "manifest_kind": "complete_task",
        "headings": {
            "required": ["# DISH", "## PROCESS RECORD"],
            "optional": ["## QUANTITIES"],
            "exactly_once": ["# DISH", "## PROCESS RECORD"],
            "allowed": ["# DISH", "## QUANTITIES", "## PROCESS RECORD"],
        },
        "labels": {
            "required": [
                "Exemptions",
                "Destination section",
                "Self-verified",
                "Verification",
            ],
            "optional": ["Portions"],
            "exactly_once": [
                "Exemptions",
                "Destination section",
                "Self-verified",
                "Verification",
            ],
            "allowed": [
                "Exemptions",
                "Destination section",
                "Self-verified",
                "Verification",
                "Portions",
            ],
        },
        "contextual_labels": [
            {"heading": "## QUANTITIES", "required_label": "Portions"},
        ],
        "exemptions": planning_manifest(version)["exemptions"],
        "destination_section": planning_manifest(version)["destination_section"],
        "title": {
            "role_tags": [
                "side",
                "dessert",
                "component",
                "condiment",
                "benchmark",
                "comparison",
            ],
            "marker_pattern": r"^[^\[\]\r\n]+$",
            "marker_prefix": "[",
            "marker_suffix": "]",
            "separator": " — ",
            "unreviewed_blocker": "blockers unreviewed",
        },
    }


def run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def write_release(repo, version="fixture-v1", *, malformed=None, mismatch=None):
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "protocol_release").write_text(f"{version}\n")
    for name, content in PROTOCOLS.items():
        (repo / name).write_text(content.replace("protocol", f"protocol {version}", 1))
    manifests = {
        "dish-planning-manifest.json": planning_manifest(version),
        "dish-complete-task-manifest.json": complete_manifest(version),
    }
    if mismatch:
        manifests[mismatch]["protocol_release"] = "wrong-version"
    for name, content in manifests.items():
        if malformed == name:
            (repo / name).write_text("{not-json\n")
        else:
            (repo / name).write_text(
                json.dumps(content, indent=2, sort_keys=True) + "\n"
            )


def commit_release(repo, message="fixture release"):
    files = [
        "DISH_VERSION",
        "dish-task-schema.json",
        "dish-schema-migrations/0001-initial.json",
        "dish-planning-protocol.md",
        "dish-research-protocol.md",
        "dish-verification-protocol.md",
        "dish-cooking-protocol.md",
    ]
    run_git(repo, "add", *files)
    run_git(repo, "commit", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


@pytest.fixture
def release_repo(tmp_path):
    repo = tmp_path / "honest-pantry"
    shutil.copytree(FIXTURE_RELEASE_DIR, repo)
    run_git(repo, "init", "-b", "main")
    run_git(repo, "config", "user.name", "Fixture")
    run_git(repo, "config", "user.email", "fixture@example.invalid")
    commit = commit_release(repo)
    return repo, commit


@pytest.mark.smoke
def test_resolver_loads_external_schema_and_requested_protocol(release_repo):
    repo, _ = release_repo
    release = resolve_release(repo, protocol_role="planning")

    assert release.protocol_version == "1.0.11"
    assert release.schema_version == "2"
    assert set(release.protocols) == {"planning"}
    assert set(release.schema["legacy_validation_adapter"]) == {"planning", "complete_task"}


@pytest.mark.smoke
def test_resolver_preserves_requested_protocol_bytes_exactly(release_repo):
    repo, _ = release_repo
    exact = "# Research protocol\nTrailing spaces stay.   \n\n"
    (repo / "dish-research-protocol.md").write_text(exact)

    release = resolve_release(repo, protocol_role="research")
    assert release.protocols["research"] == exact

