"""Mechanical contracts for the canonical architecture knowledge base."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = ROOT / "docs" / "architecture"
INDEX = ARCHITECTURE / "index.md"
DOMAIN_DOCUMENTS = (
    "system-context.md",
    "authority-and-data-ownership.md",
    "packages-and-dependencies.md",
    "commands-and-surfaces.md",
    "workflow-and-human-review.md",
    "request-replay-and-idempotency.md",
    "operations-leases-and-fencing.md",
    "external-effects-and-asana.md",
    "postgresql-runtime.md",
    "dark-launch.md",
    "testing-boundaries.md",
    "extension-rules.md",
)
MANDATORY_HEADINGS = (
    "Read this when",
    "Scope",
    "Authoritative implementation",
    "Actors, processes, and stores",
    "Authority and data ownership",
    "Invariants",
    "Process and transaction boundaries",
    "Normal flow",
    "Failure, replay, recovery, and concurrency",
    "Change routing",
    "Proving tests",
    "Current debt and temporary compatibility",
    "Related documents",
)
REPOSITORY_PATH_PREFIXES = (
    "dish_tool/",
    "dish_service/",
    "dish_pg/",
    "dish_shadow/",
    "tests/",
    "scripts/",
    "test_selection/",
    "deploy/",
    "frontend/",
    "openapi/",
    "docs/",
)


def _documents() -> list[Path]:
    return sorted(ARCHITECTURE.rglob("*.md"))


def _local_markdown_targets(path: Path) -> list[str]:
    targets: list[str] = []
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
        target = target.split("#", 1)[0]
        if target and "://" not in target and not target.startswith("mailto:"):
            targets.append(target)
    return targets


def _repository_paths(path: Path) -> set[str]:
    references: set[str] = set()
    for value in re.findall(r"`([^`\n]+)`", path.read_text(encoding="utf-8")):
        candidate = value.strip().rstrip(".,;:")
        if candidate.startswith(REPOSITORY_PATH_PREFIXES):
            references.add(candidate)
    return references


def test_index_lists_every_architecture_document() -> None:
    index = INDEX.read_text(encoding="utf-8")
    missing = [
        path.relative_to(ARCHITECTURE).as_posix()
        for path in _documents()
        if path != INDEX and f"({path.relative_to(ARCHITECTURE).as_posix()})" not in index
    ]
    assert missing == []


@pytest.mark.parametrize("path", _documents(), ids=lambda path: path.relative_to(ROOT).as_posix())
def test_local_architecture_links_resolve(path: Path) -> None:
    broken = [
        target
        for target in _local_markdown_targets(path)
        if not (path.parent / target).resolve().exists()
    ]
    assert broken == []


@pytest.mark.parametrize("path", _documents(), ids=lambda path: path.relative_to(ROOT).as_posix())
def test_referenced_source_and_test_paths_exist(path: Path) -> None:
    missing = sorted(reference for reference in _repository_paths(path) if not (ROOT / reference).exists())
    assert missing == []


def test_architecture_index_is_a_complete_router() -> None:
    index = INDEX.read_text(encoding="utf-8")
    for heading in (
        "One-page system overview",
        "Current authority summary",
        "Start here for…",
        "Task-to-document routing",
        "Subsystem-to-authoritative-code map",
        "Document status and ownership",
        "Runbooks, product decisions, and active plans",
    ):
        assert f"## {heading}" in index
    assert "Runbooks describe operations; architecture documents describe ownership and invariants." in index


@pytest.mark.parametrize("name", DOMAIN_DOCUMENTS)
def test_domain_documents_have_the_mandatory_contract(name: str) -> None:
    text = (ARCHITECTURE / name).read_text(encoding="utf-8")
    for heading in MANDATORY_HEADINGS:
        assert f"## {heading}" in text


@pytest.mark.parametrize(
    "path",
    sorted((ARCHITECTURE / "decisions").glob("*.md")),
    ids=lambda path: path.relative_to(ROOT).as_posix(),
)
def test_decision_documents_use_the_same_navigation_contract(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for heading in MANDATORY_HEADINGS:
        assert f"## {heading}" in text


def test_required_diagrams_are_text_based() -> None:
    texts = {path.name: path.read_text(encoding="utf-8") for path in _documents()}
    assert texts["index.md"].count("```mermaid") >= 1
    assert texts["system-context.md"].count("```mermaid") >= 1
    assert texts["commands-and-surfaces.md"].count("```mermaid") >= 1
    assert texts["request-replay-and-idempotency.md"].count("```mermaid") >= 1
    assert texts["external-effects-and-asana.md"].count("```mermaid") >= 1
    assert texts["dark-launch.md"].count("```mermaid") >= 1
    assert sum(text.count("```mermaid") for text in texts.values()) >= 6
    assert not any(re.search(r"!\[[^\]]*\]\([^)]+\)", text) for text in texts.values())


def test_entry_instructions_route_to_the_canonical_index() -> None:
    for path in (ROOT.parent / "AGENTS.md", ROOT.parent / "CLAUDE.md"):
        assert "dish/docs/architecture/index.md" in path.read_text(encoding="utf-8")


def test_old_architecture_file_is_redirect_only() -> None:
    redirect = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "architecture/index.md" in redirect
    assert len(redirect.splitlines()) <= 8
    assert "## Invariants" not in redirect
    assert "## Authority and data ownership" not in redirect


def test_repository_routes_to_one_architecture_authority() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/architecture/index.md" in readme
    assert "production migration and cutover are complete" not in readme.lower()

    stale: list[str] = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        if path == ROOT / "docs" / "architecture.md" or ARCHITECTURE in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if "docs/architecture.md" in text or "(architecture.md)" in text:
            stale.append(path.relative_to(ROOT).as_posix())
    assert stale == []


def test_canonical_docs_exclude_prohibited_paths_and_delivery_metadata() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in _documents()).lower()
    for prohibited in (
        "docs/postgresql-cutover/",
        "ai-tools-venv",
        "synthetic baseline",
        "synthetic base",
        "format-patch",
        "delivery report",
        "source-worktree-manifest",
    ):
        assert prohibited not in combined


def test_current_dark_launch_architecture_survives_monolith_retirement() -> None:
    text = (ARCHITECTURE / "dark-launch.md").read_text(encoding="utf-8")
    for required in (
        "dish_pg/location_manifest.py",
        "dish_pg/dark_launch_readiness.py",
        "dish_pg/legacy_source.py",
        "mode=ro",
        "systemctl show",
        "read-only transaction",
        "disabled and inactive/stopped",
        "complete location manifest",
        "fixed production service environment",
    ):
        assert required in text
    assert "cannot create authority" in text
    assert "no Asana I/O" in text


def test_current_postgresql_validation_and_reconciliation_ownership_is_documented() -> None:
    runtime = (ARCHITECTURE / "postgresql-runtime.md").read_text(encoding="utf-8")
    replay = (ARCHITECTURE / "request-replay-and-idempotency.md").read_text(encoding="utf-8")
    for required in (
        "0031_worker_readiness_consolidation.py",
        "record_replay_validation_failure",
        "record_validation_failure",
        "first-request reservation",
        "start_reconciliation",
        "record_reconciliation_item",
        "complete_reconciliation",
        "database-backend-postgresql-test-plan.md",
        "ops-issues.md",
    ):
        assert required in runtime
    assert "validation-only" in replay
    assert "reconciliation" in replay.lower()
    assert "not evidence that the current §3 or §4 rehearsal has passed" in runtime


def test_architecture_redirect_happens_after_surviving_fact_owners_exist() -> None:
    redirect = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert len(redirect.splitlines()) <= 8
    assert (ARCHITECTURE / "dark-launch.md").exists()
    assert (ARCHITECTURE / "postgresql-runtime.md").exists()
    assert (ARCHITECTURE / "request-replay-and-idempotency.md").exists()
