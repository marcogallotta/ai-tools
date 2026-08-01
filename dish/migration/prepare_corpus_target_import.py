#!/usr/bin/env python3
"""Prepare an idempotent production Cooking-project task-creation batch.

The script is read-only against Asana.  It validates the accepted migration
archives and Marco-approved status file, checks source drift, validates an
empty-or-partially-created target project, and emits only the missing guarded
``asana batch-apply`` create operations.  Exact existing targets are reused;
duplicates, unexpected tasks, or drift stop the run.

Once all targets exist, a zero-operation rerun emits the authoritative
source-to-target mapping and the durable-state assignment file used by
``import_migrated_durable_state.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from migration.audit_asana_side_data import AsanaReader
from dish_tool.task_document import (
    DocumentParseError,
    parse_task_document,
    validate_task_document,
)


SOURCE_PROJECT_GID = "1215089183018968"
TEST_PROJECT_GID = "1216693403164366"
EXPECTED_GOVERNED = 99
EXPECTED_UNMANAGED = 4
RUNTIME_REQUIRED_SECTIONS = {"Research Queue", "Reference"}
CORRECTION_ARCHIVE_SHA256 = (
    "c3a2ce255fc50f2085e3bb9c03b658061bfbbfef4daf3ec6325296fc6454505f"
)
LEGACY_ARCHIVE_SHA256 = (
    "67be5a8bb115e847ddf6a29be3fb846eb76fc81d64eb6958231a84ed04f544b9"
)
CORRECTION_ROOT = "dish_migration_batch_002_correction_4"
LEGACY_ROOT = "dish_migration_pre_batch_002_v3"
STATE_PLACEHOLDER = "{{MIGRATED_DISH_STATE_BLOCK}}"
SECTION_PLACEHOLDER = "{{TARGET_SECTION_GID}}"
STATE_FIELDS = (
    "Status",
    "Status detail",
    "Resume status",
    "Verification protocol release",
    "Researched by",
    "Verified by",
    "Self-verified",
)
ALLOWED_STATUSES = {
    "pending-research",
    "pending-evidence",
    "pending-human-review",
    "pending-verification",
    "ready",
}


class PreparationFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class GovernedTask:
    source_gid: str
    source_order: int
    source_name: str
    source_modified_at: str
    source_notes_sha256: str
    source_section_name: str
    destination_section_name: str
    template_file: str
    template_sha256: str
    status: str
    rationale: str
    title: str | None = None
    notes: str | None = None
    final_sha256: str | None = None


@dataclass(frozen=True)
class UnmanagedTask:
    source_gid: str
    source_order: int
    source_name: str
    notes: str
    notes_sha256: str


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PreparationFailure(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PreparationFailure(f"invalid JSON in {path}: {exc}") from exc


def as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PreparationFailure(f"{label} must be a JSON object")
    return value


def require_archive(path: Path, expected_sha256: str, label: str) -> tarfile.TarFile:
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise PreparationFailure(
            f"{label} SHA-256 {actual} != accepted {expected_sha256}"
        )
    return tarfile.open(path, "r:gz")


def read_archive_member(tf: tarfile.TarFile, member: str) -> bytes:
    pure = PurePosixPath(member)
    if pure.is_absolute() or ".." in pure.parts:
        raise PreparationFailure(f"unsafe archive member path: {member}")
    extracted = tf.extractfile(member)
    if extracted is None:
        raise PreparationFailure(f"archive member missing or not a file: {member}")
    return extracted.read()


def load_archive_json(tf: tarfile.TarFile, member: str) -> Any:
    try:
        return json.loads(read_archive_member(tf, member).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationFailure(f"invalid JSON archive member {member}: {exc}") from exc


def load_approved_statuses(
    path: Path, expected_sha256: str,
) -> Mapping[str, Mapping[str, Any]]:
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise PreparationFailure(
            f"approved-status file SHA-256 {actual} != Marco-approved {expected_sha256}"
        )
    raw = load_json(path)
    if not isinstance(raw, list) or len(raw) != EXPECTED_GOVERNED:
        raise PreparationFailure("approved-status file must contain exactly 99 rows")
    result: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(raw, start=1):
        row = as_mapping(value, f"approved-status row {index}")
        gid = str(row.get("source_gid") or "").strip()
        status = str(row.get("proposed_status") or "").strip()
        rationale = str(row.get("concise_reason") or "").strip()
        if not gid.isdigit() or gid in result:
            raise PreparationFailure(f"invalid or duplicate approved source_gid: {gid!r}")
        if status not in ALLOWED_STATUSES:
            raise PreparationFailure(f"{gid}: unsupported approved status {status!r}")
        if not rationale:
            raise PreparationFailure(f"{gid}: approved status lacks concise_reason")
        if str(row.get("confidence") or "") != "high":
            raise PreparationFailure(f"{gid}: approved status confidence is not high")
        result[gid] = row
    return result


def load_governed(
    tf: tarfile.TarFile, statuses: Mapping[str, Mapping[str, Any]],
) -> list[GovernedTask]:
    manifest = load_archive_json(
        tf, f"{CORRECTION_ROOT}/manifest-batch-002.json"
    )
    if not isinstance(manifest, list) or len(manifest) != EXPECTED_GOVERNED:
        raise PreparationFailure("Correction 4 manifest must contain exactly 99 rows")
    tasks: list[GovernedTask] = []
    seen: set[str] = set()
    for row_value in manifest:
        row = as_mapping(row_value, "Correction 4 manifest row")
        gid = str(row.get("source_gid") or "").strip()
        if not gid.isdigit() or gid in seen:
            raise PreparationFailure(f"invalid or duplicate governed source_gid: {gid!r}")
        seen.add(gid)
        status_row = statuses.get(gid)
        if status_row is None:
            raise PreparationFailure(f"{gid}: no approved production status")
        source_name = str(row.get("source_name") or "").strip()
        if str(status_row.get("task_name") or "").strip() != source_name:
            raise PreparationFailure(f"{gid}: approved task name differs from manifest")
        destination = str(
            row.get("proposed_target_section_name")
            or row.get("captured_section_name")
            or ""
        ).strip()
        task = GovernedTask(
            source_gid=gid,
            source_order=int(row.get("source_order")),
            source_name=source_name,
            source_modified_at=str(row.get("source_modified_at") or "").strip(),
            source_notes_sha256=str(row.get("source_notes_sha256") or "").strip(),
            source_section_name=str(row.get("captured_section_name") or "").strip(),
            destination_section_name=destination,
            template_file=str(row.get("template_file") or "").strip(),
            template_sha256=str(row.get("template_sha256") or "").strip(),
            status=str(status_row["proposed_status"]),
            rationale=str(status_row["concise_reason"]),
        )
        if not all((
            task.source_name,
            task.source_modified_at,
            task.source_notes_sha256,
            task.source_section_name,
            task.destination_section_name,
            task.template_file,
            task.template_sha256,
        )):
            raise PreparationFailure(f"{gid}: incomplete governed manifest row")
        tasks.append(task)
    if set(statuses) != seen:
        raise PreparationFailure("approved-status coverage differs from Correction 4")
    return sorted(tasks, key=lambda item: item.source_order)


def load_unmanaged(tf: tarfile.TarFile) -> list[UnmanagedTask]:
    manifest = load_archive_json(tf, f"{LEGACY_ROOT}/manifest-full-110.json")
    if not isinstance(manifest, list):
        raise PreparationFailure("legacy full manifest must be an array")
    tasks: list[UnmanagedTask] = []
    for row_value in manifest:
        row = as_mapping(row_value, "legacy full-manifest row")
        if row.get("disposition") != "copy_unmanaged":
            continue
        gid = str(row.get("source_gid") or "").strip()
        member = f"{LEGACY_ROOT}/unmanaged/{gid}.txt"
        notes_bytes = read_archive_member(tf, member)
        try:
            notes = notes_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PreparationFailure(f"{gid}: unmanaged notes are not UTF-8: {exc}") from exc
        tasks.append(UnmanagedTask(
            source_gid=gid,
            source_order=int(row.get("source_order")),
            source_name=str(row.get("source_name") or "").strip(),
            notes=notes,
            notes_sha256=sha256_bytes(notes_bytes),
        ))
    if len(tasks) != EXPECTED_UNMANAGED:
        raise PreparationFailure("legacy archive must contain exactly four unmanaged copies")
    if len({item.source_gid for item in tasks}) != EXPECTED_UNMANAGED:
        raise PreparationFailure("unmanaged source GIDs are not unique")
    return sorted(tasks, key=lambda item: item.source_order)


def state_assignment(task: GovernedTask, target_gid: str | None = None) -> dict[str, Any]:
    migration_actor = "Codex — migration-assigned baseline, 2026-08-01"
    migration_release = "migration-assigned baseline, 2026-08-01"
    detail = (
        "Migration-assigned initial production state approved by Marco on "
        "2026-08-01; no live Dish workflow cycle was performed."
    )
    assignment: dict[str, Any] = {
        "Status": task.status,
        "Status detail": "None",
        "Resume status": "None",
        "Verification protocol release": "None",
        "Researched by": "None",
        "Verified by": "None",
        "Self-verified": "None",
        "authority": "Marco-approved corpus migration status assignment, 2026-08-01.",
        "rationale": task.rationale,
        "live_research_cycle_produced": False,
        "live_verification_cycle_produced": False,
    }
    if task.status == "pending-research":
        assignment["Status detail"] = detail
    elif task.status in {"pending-evidence", "pending-human-review"}:
        assignment["Status detail"] = detail
        assignment["Resume status"] = "pending-research"
    elif task.status == "pending-verification":
        assignment["Verification protocol release"] = migration_release
        assignment["Self-verified"] = migration_actor
    elif task.status == "ready":
        assignment["Verification protocol release"] = migration_release
        assignment["Verified by"] = migration_actor
        assignment["Self-verified"] = migration_actor
    else:
        raise PreparationFailure(f"{task.source_gid}: unsupported status {task.status!r}")
    if target_gid is not None:
        assignment["target_gid"] = target_gid
    return assignment


def render_governed(
    tf: tarfile.TarFile,
    tasks: list[GovernedTask],
    registry: Mapping[str, str],
    schema: Mapping[str, Any],
) -> list[GovernedTask]:
    schema_version = str(schema.get("schema_version") or "").strip()
    if not schema_version:
        raise PreparationFailure("Dish schema does not declare schema_version")
    rendered_tasks: list[GovernedTask] = []
    failures: list[str] = []
    for task in tasks:
        section_gid = registry.get(task.destination_section_name)
        if section_gid is None:
            failures.append(
                f"{task.source_gid}: registry lacks {task.destination_section_name!r}"
            )
            continue
        member = f"{CORRECTION_ROOT}/{task.template_file}"
        template_bytes = read_archive_member(tf, member)
        if sha256_bytes(template_bytes) != task.template_sha256:
            failures.append(f"{task.source_gid}: template SHA-256 differs from manifest")
            continue
        template = template_bytes.decode("utf-8")
        if template.count(STATE_PLACEHOLDER) != 1:
            failures.append(f"{task.source_gid}: state placeholder count is not one")
            continue
        if template.count(SECTION_PLACEHOLDER) != 1:
            failures.append(f"{task.source_gid}: section placeholder count is not one")
            continue
        assignment = state_assignment(task)
        state_block = "\n".join(
            f"{field}: {str(assignment[field]).strip()}" for field in STATE_FIELDS
        )
        destination_pattern = re.compile(
            r"(?m)^Destination section: .+? — " + re.escape(SECTION_PLACEHOLDER) + r"$"
        )
        if len(destination_pattern.findall(template)) != 1:
            failures.append(f"{task.source_gid}: canonical destination line is missing or duplicated")
            continue
        template = destination_pattern.sub(
            f"Destination section: {task.destination_section_name} — {SECTION_PLACEHOLDER}",
            template,
        )
        resolved = template.replace(STATE_PLACEHOLDER, state_block)
        resolved = resolved.replace(SECTION_PLACEHOLDER, section_gid)
        resolved = resolved.rstrip("\n") + f"\nSchema version: {schema_version}\n"
        try:
            document = parse_task_document(resolved)
        except DocumentParseError as exc:
            failures.append(
                f"{task.source_gid}: canonical parse failed [{exc.rule}]: {exc}"
            )
            continue
        validation = validate_task_document(
            document,
            expected_schema_version=schema_version,
            schema=schema,
        )
        if not validation.ok:
            detail = "; ".join(
                f"{item.rule} at {item.location or 'document'}: {item.message}"
                for item in validation.findings
            )
            failures.append(f"{task.source_gid}: {detail}")
            continue
        canonical = document.render()
        if "\n" not in canonical:
            failures.append(f"{task.source_gid}: rendered document lacks title/body split")
            continue
        title, notes = canonical.split("\n", 1)
        if not notes.endswith("\n"):
            notes += "\n"
        rendered_tasks.append(GovernedTask(
            **{
                **task.__dict__,
                "title": title,
                "notes": notes,
                "final_sha256": sha256_bytes((title + "\n" + notes).encode("utf-8")),
            }
        ))
    if failures:
        raise PreparationFailure("\n".join(failures))
    if len(rendered_tasks) != EXPECTED_GOVERNED:
        raise PreparationFailure("did not render exactly 99 governed tasks")
    return rendered_tasks


def expected_sections(tasks: list[GovernedTask]) -> list[str]:
    return sorted(
        {task.destination_section_name for task in tasks}
        | {"Sourcing"}
        | RUNTIME_REQUIRED_SECTIONS
    )


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_blueprint(
    *, output_dir: Path, governed: list[GovernedTask], unmanaged: list[UnmanagedTask],
    archive_sha256: str, legacy_sha256: str, statuses_sha256: str,
) -> dict[str, Any]:
    section_counts = Counter(task.destination_section_name for task in governed)
    section_counts["Sourcing"] = len(unmanaged)
    blueprint = {
        "kind": "dish-production-project-blueprint",
        "project_name": "Cooking",
        "source_project_gid": SOURCE_PROJECT_GID,
        "excluded_test_project_gid": TEST_PROJECT_GID,
        "sections": [
            {"name": name, "task_count": section_counts[name]}
            for name in expected_sections(governed)
        ],
        "governed_tasks": EXPECTED_GOVERNED,
        "unmanaged_sourcing_tasks": EXPECTED_UNMANAGED,
        "total_tasks": EXPECTED_GOVERNED + EXPECTED_UNMANAGED,
        "status_counts": dict(sorted(Counter(task.status for task in governed).items())),
        "inputs": {
            "correction_archive_sha256": archive_sha256,
            "legacy_archive_sha256": legacy_sha256,
            "approved_statuses_sha256": statuses_sha256,
        },
        "tasks": [
            {
                "source_gid": task.source_gid,
                "source_order": task.source_order,
                "name": task.source_name,
                "kind": "governed",
                "destination_section_name": task.destination_section_name,
                "status": task.status,
                "template_sha256": task.template_sha256,
            }
            for task in governed
        ] + [
            {
                "source_gid": task.source_gid,
                "source_order": task.source_order,
                "name": task.source_name,
                "kind": "unmanaged",
                "destination_section_name": "Sourcing",
                "notes_sha256": task.notes_sha256,
            }
            for task in unmanaged
        ],
    }
    write_json(output_dir / "production-project-blueprint.json", blueprint)
    return blueprint


def load_registry(path: Path, required: list[str]) -> Mapping[str, str]:
    raw = as_mapping(load_json(path), "section registry")
    registry = {str(name).strip(): str(gid).strip() for name, gid in raw.items()}
    if set(registry) != set(required):
        raise PreparationFailure(
            f"section registry names differ; missing={sorted(set(required)-set(registry))}, "
            f"extra={sorted(set(registry)-set(required))}"
        )
    if any(not gid.isdigit() for gid in registry.values()):
        raise PreparationFailure("section registry contains a non-numeric GID")
    if len(set(registry.values())) != len(registry):
        raise PreparationFailure("section registry contains duplicate GIDs")
    return registry


def source_membership(detail: Mapping[str, Any], project_gid: str) -> str | None:
    matches = [
        item for item in (detail.get("memberships") or [])
        if str((item.get("project") or {}).get("gid") or "") == project_gid
    ]
    if len(matches) != 1:
        return None
    section = matches[0].get("section") or {}
    return str(section.get("name") or "").strip() or None


def validate_sources(
    reader: AsanaReader,
    governed: list[GovernedTask],
    unmanaged: list[UnmanagedTask],
    overrides: Mapping[str, Any],
) -> None:
    failures: list[str] = []
    for task in governed:
        detail = reader.get(
            f"/tasks/{task.source_gid}",
            {"opt_fields": (
                "gid,name,notes,modified_at,parent.gid,completed,due_on,due_at,"
                "memberships.project.gid,memberships.section.name"
            )},
        )["data"]
        override_value = overrides.get(task.source_gid)
        override = as_mapping(override_value, f"source override {task.source_gid}") if override_value else None
        expected_modified = (
            str(override.get("expected_modified_at")) if override else task.source_modified_at
        )
        expected_section = (
            str(override.get("expected_section_name")) if override else task.source_section_name
        )
        expected_notes_hash = (
            str(override.get("notes_sha256")) if override else task.source_notes_sha256
        )
        if detail.get("name") != task.source_name:
            failures.append(f"{task.source_gid}: source name drift")
        if detail.get("modified_at") != expected_modified:
            failures.append(f"{task.source_gid}: source modified_at drift")
        if sha256_bytes((detail.get("notes") or "").encode("utf-8")) != expected_notes_hash:
            failures.append(f"{task.source_gid}: source notes drift")
        if source_membership(detail, SOURCE_PROJECT_GID) != expected_section:
            failures.append(f"{task.source_gid}: source section drift")
        if detail.get("parent") is not None:
            failures.append(f"{task.source_gid}: source task is no longer top-level")
    if set(overrides) - {task.source_gid for task in governed}:
        failures.append("source override file contains an out-of-scope GID")

    for task in unmanaged:
        detail = reader.get(
            f"/tasks/{task.source_gid}",
            {"opt_fields": (
                "gid,name,notes,parent.gid,completed,due_on,due_at,"
                "memberships.project.gid,memberships.section.name"
            )},
        )["data"]
        if detail.get("name") != task.source_name:
            failures.append(f"{task.source_gid}: unmanaged source name drift")
        if sha256_bytes((detail.get("notes") or "").encode("utf-8")) != task.notes_sha256:
            failures.append(f"{task.source_gid}: unmanaged source notes drift")
        if source_membership(detail, SOURCE_PROJECT_GID) != "Sourcing":
            failures.append(f"{task.source_gid}: unmanaged source section drift")
        if detail.get("parent") is not None or detail.get("completed") is not False:
            failures.append(f"{task.source_gid}: unmanaged source shape drift")
        if detail.get("due_on") is not None or detail.get("due_at") is not None:
            failures.append(f"{task.source_gid}: unmanaged due-date copy is not planned")
    if failures:
        raise PreparationFailure("\n".join(failures))


def validate_target_sections(
    reader: AsanaReader, project_gid: str, registry: Mapping[str, str],
) -> None:
    project = reader.get(
        f"/projects/{project_gid}", {"opt_fields": "gid,name,archived"}
    )["data"]
    if project.get("archived") is True:
        raise PreparationFailure("target project is archived")
    sections = reader.pages(
        f"/projects/{project_gid}/sections", "gid,name,project.gid"
    )["items"]
    by_name: dict[str, list[str]] = {}
    for section in sections:
        by_name.setdefault(str(section.get("name") or ""), []).append(str(section["gid"]))
    if set(by_name) != set(registry):
        raise PreparationFailure(
            f"live target sections differ; missing={sorted(set(registry)-set(by_name))}, "
            f"extra={sorted(set(by_name)-set(registry))}"
        )
    for name, gid in registry.items():
        if by_name[name] != [gid]:
            raise PreparationFailure(f"target section {name!r} does not match registry exactly")


def target_section_gid(detail: Mapping[str, Any], project_gid: str) -> str | None:
    matches = [
        item for item in (detail.get("memberships") or [])
        if str((item.get("project") or {}).get("gid") or "") == project_gid
    ]
    if len(matches) != 1:
        return None
    section = matches[0].get("section") or {}
    return str(section.get("gid") or "").strip() or None


def prepare_target(
    *, reader: AsanaReader, project_gid: str, registry: Mapping[str, str],
    governed: list[GovernedTask], unmanaged: list[UnmanagedTask], output_dir: Path,
    input_hashes: Mapping[str, str],
) -> tuple[int, int]:
    expected = []
    for task in governed:
        expected.append({
            "source_gid": task.source_gid,
            "kind": "governed",
            "name": task.title,
            "notes": task.notes,
            "section_name": task.destination_section_name,
            "section_gid": registry[task.destination_section_name],
            "task": task,
        })
    for task in unmanaged:
        expected.append({
            "source_gid": task.source_gid,
            "kind": "unmanaged",
            "name": task.source_name,
            "notes": task.notes,
            "section_name": "Sourcing",
            "section_gid": registry["Sourcing"],
            "task": task,
        })
    names = [str(item["name"]) for item in expected]
    if len(names) != len(set(names)):
        raise PreparationFailure("expected target task names are not unique")

    live = reader.pages(
        f"/projects/{project_gid}/tasks",
        "gid,name,notes,parent.gid,completed,memberships.project.gid,memberships.section.gid",
    )["items"]
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for task in live:
        by_name.setdefault(str(task.get("name") or ""), []).append(task)
    unexpected = sorted(set(by_name) - set(names))
    if unexpected:
        raise PreparationFailure(f"target project contains unexpected tasks: {unexpected}")

    operations = []
    mapping: dict[str, Any] = {}
    unmanaged_mapping: dict[str, Any] = {}
    for item in expected:
        matches = by_name.get(str(item["name"]), [])
        if len(matches) > 1:
            raise PreparationFailure(f"duplicate target name: {item['name']!r}")
        if not matches:
            operations.append({
                "action": "create_task",
                "project": project_gid,
                "name": item["name"],
                "section": item["section_gid"],
                "notes": item["notes"],
                "reason": f"Copy migration source task {item['source_gid']} into the new production Cooking project.",
            })
            continue
        live_task = matches[0]
        target_gid = str(live_task["gid"])
        if live_task.get("parent") is not None or live_task.get("completed") is not False:
            raise PreparationFailure(f"{target_gid}: existing target shape differs")
        if (live_task.get("notes") or "") != item["notes"]:
            raise PreparationFailure(f"{target_gid}: existing target notes differ")
        if target_section_gid(live_task, project_gid) != item["section_gid"]:
            raise PreparationFailure(f"{target_gid}: existing target section differs")
        entry = {
            "target_gid": target_gid,
            "section_gid": item["section_gid"],
            "section_name": item["section_name"],
            "notes_sha256": sha256_bytes(str(item["notes"]).encode("utf-8")),
        }
        if item["kind"] == "governed":
            task = item["task"]
            entry.update({
                "production_status": task.status,
                "template_sha256": task.template_sha256,
                "final_document_sha256": task.final_sha256,
            })
            mapping[str(item["source_gid"])] = entry
        else:
            unmanaged_mapping[str(item["source_gid"])] = entry

    plan = {
        "plan_metadata": {
            "kind": "dish-production-corpus-task-creation",
            "environment": "production",
            "target_project_gid": project_gid,
            "source_project_gid": SOURCE_PROJECT_GID,
            "inputs": dict(input_hashes),
            "expected_tasks": len(expected),
            "existing_exact_targets": len(mapping) + len(unmanaged_mapping),
            "missing_targets": len(operations),
            "api_requests": reader.requests,
        },
        "operations": operations,
    }
    write_json(output_dir / "production-task-creation-plan.json", plan)
    if not operations:
        if len(mapping) != EXPECTED_GOVERNED or len(unmanaged_mapping) != EXPECTED_UNMANAGED:
            raise PreparationFailure("zero-operation target does not have complete mappings")
        write_json(output_dir / "production-source-target-mapping.json", mapping)
        write_json(output_dir / "production-unmanaged-mapping.json", unmanaged_mapping)
        durable = {
            task.source_gid: state_assignment(task, mapping[task.source_gid]["target_gid"])
            for task in governed
        }
        write_json(output_dir / "production-durable-state-assignments.json", durable)
    return len(operations), len(mapping) + len(unmanaged_mapping)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-archive", required=True, type=Path)
    parser.add_argument("--legacy-archive", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--approved-statuses", required=True, type=Path)
    parser.add_argument("--approved-statuses-sha256", required=True)
    parser.add_argument("--source-overrides", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--blueprint-only", action="store_true")
    parser.add_argument("--target-project-gid")
    parser.add_argument("--section-registry", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    reader: AsanaReader | None = None
    try:
        batch_archive = args.batch_archive.expanduser().resolve()
        legacy_archive = args.legacy_archive.expanduser().resolve()
        statuses_path = args.approved_statuses.expanduser().resolve()
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        statuses = load_approved_statuses(
            statuses_path, args.approved_statuses_sha256.strip()
        )
        schema = as_mapping(load_json(args.schema.expanduser().resolve()), "Dish schema")
        overrides = as_mapping(
            load_json(args.source_overrides.expanduser().resolve()), "source overrides"
        )
        with require_archive(
            batch_archive, CORRECTION_ARCHIVE_SHA256, "Correction 4 archive"
        ) as batch_tf, require_archive(
            legacy_archive, LEGACY_ARCHIVE_SHA256, "legacy archive"
        ) as legacy_tf:
            governed = load_governed(batch_tf, statuses)
            unmanaged = load_unmanaged(legacy_tf)
            input_hashes = {
                "correction_archive_sha256": sha256_file(batch_archive),
                "legacy_archive_sha256": sha256_file(legacy_archive),
                "approved_statuses_sha256": sha256_file(statuses_path),
                "source_overrides_sha256": sha256_file(args.source_overrides.expanduser().resolve()),
                "schema_sha256": sha256_file(args.schema.expanduser().resolve()),
            }
            write_blueprint(
                output_dir=output_dir,
                governed=governed,
                unmanaged=unmanaged,
                archive_sha256=input_hashes["correction_archive_sha256"],
                legacy_sha256=input_hashes["legacy_archive_sha256"],
                statuses_sha256=input_hashes["approved_statuses_sha256"],
            )
            if args.blueprint_only:
                print("PASS production blueprint: 99 governed + 4 unmanaged tasks")
                return 0
            project_gid = str(args.target_project_gid or "").strip()
            if not project_gid.isdigit() or project_gid in {SOURCE_PROJECT_GID, TEST_PROJECT_GID}:
                raise PreparationFailure("target project GID is missing, invalid, source, or test")
            if args.section_registry is None:
                raise PreparationFailure("--section-registry is required outside --blueprint-only")
            registry = load_registry(
                args.section_registry.expanduser().resolve(), expected_sections(governed)
            )
            rendered = render_governed(batch_tf, governed, registry, schema)
            reader = AsanaReader()
            validate_sources(reader, rendered, unmanaged, overrides)
            validate_target_sections(reader, project_gid, registry)
            missing, existing = prepare_target(
                reader=reader,
                project_gid=project_gid,
                registry=registry,
                governed=rendered,
                unmanaged=unmanaged,
                output_dir=output_dir,
                input_hashes=input_hashes,
            )
        print(
            f"PASS production target checked: existing={existing}, missing={missing}; "
            f"operations={missing}"
        )
        return 0
    except (OSError, PreparationFailure) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
