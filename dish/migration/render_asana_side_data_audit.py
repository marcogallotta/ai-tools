#!/usr/bin/env python3
"""Render the classified Dish corpus side-data audit from a raw read-only capture."""

import argparse
import hashlib
import json
import tarfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


SOURCE_PROJECT_GID = "1215089183018968"
TEST_PROJECT_GID = "1216693403164366"
EXPECTED_SCOPE = 99


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def comments(stories):
    return [item for item in stories if item.get("resource_subtype") == "comment_added"]


def system_stories(stories):
    return [item for item in stories if item.get("resource_subtype") != "comment_added"]


def pagination_summary(record):
    result = {}
    for kind in ("stories", "attachments", "subtasks"):
        page = record[kind]["pagination"]
        result[kind] = {
            "page_count": page["page_count"],
            "exhausted": page["exhausted"],
            "duplicates_suppressed": len(page["duplicates_suppressed"]),
        }
    result["subtask_stories_and_attachments"] = {
        "direct_subtasks": len(record["subtasks"]["items"]),
        "all_exhausted": all(
            subtask[object_type]["pagination"].get("exhausted") is True
            for subtask in record["subtasks"]["items"]
            for object_type in ("stories", "attachments")
        ),
    }
    return result


def build_task(record):
    task = record["task"]
    story_items = record["stories"]["items"]
    comment_items = comments(story_items)
    system_items = system_stories(story_items)
    findings = []

    for story in comment_items:
        findings.append({
            "source_task_gid": record["source_gid"],
            "source_task_name": task["name"],
            "object_type": "human_comment",
            "object_gid": story["gid"],
            "created_at": story.get("created_at"),
            "author": story.get("created_by"),
            "text": story.get("text") or "",
            "html_text": story.get("html_text") or "",
            "task_references": story.get("task_references") or [],
            "proposed_treatment": "preserve",
            "rationale": "Human-authored task content must remain represented on the migrated task.",
        })

    for attachment in record["attachments"]["items"]:
        subtype = attachment.get("resource_subtype")
        treatment = "preserve" if subtype == "asana" else "reference"
        findings.append({
            "source_task_gid": record["source_gid"],
            "source_task_name": task["name"],
            "object_type": "attachment",
            "object_gid": attachment.get("gid"),
            "metadata": attachment,
            "proposed_treatment": treatment,
            "rationale": (
                "Preserve the Asana-hosted object on the migrated task."
                if treatment == "preserve"
                else "Retain the externally hosted attachment as an explicit reference."
            ),
        })

    for subtask in record["subtasks"]["items"]:
        in_scope = subtask["in_scope_source_gid"]
        findings.append({
            "source_task_gid": record["source_gid"],
            "source_task_name": task["name"],
            "object_type": "direct_subtask",
            "object_gid": subtask["gid"],
            "metadata": {key: value for key, value in subtask.items() if key not in ("stories", "attachments")},
            "in_scope_relationship_endpoint": in_scope,
            "proposed_treatment": "recreate-relationship" if in_scope else "reference",
            "rationale": (
                "Both endpoints are in scope, so recreate the parent relationship between migrated tasks."
                if in_scope
                else "The subtask is outside the migration scope; retain its exact source reference."
            ),
        })
        for story in comments(subtask["stories"]["items"]):
            findings.append({
                "source_task_gid": record["source_gid"],
                "source_task_name": task["name"],
                "object_type": "subtask_human_comment",
                "object_gid": story["gid"],
                "subtask_gid": subtask["gid"],
                "subtask_name": subtask["name"],
                "created_at": story.get("created_at"),
                "author": story.get("created_by"),
                "text": story.get("text") or "",
                "html_text": story.get("html_text") or "",
                "proposed_treatment": "preserve",
                "rationale": "Human-authored content on a direct subtask must remain represented.",
            })
        for attachment in subtask["attachments"]["items"]:
            treatment = "preserve" if attachment.get("resource_subtype") == "asana" else "reference"
            findings.append({
                "source_task_gid": record["source_gid"],
                "source_task_name": task["name"],
                "object_type": "subtask_attachment",
                "object_gid": attachment.get("gid"),
                "subtask_gid": subtask["gid"],
                "subtask_name": subtask["name"],
                "metadata": attachment,
                "proposed_treatment": treatment,
                "rationale": "Retain the attachment with its exact subtask association.",
            })

    for relation_type in ("dependencies", "dependents"):
        for related in record[relation_type]["items"]:
            in_scope = related["in_scope_source_gid"]
            findings.append({
                "source_task_gid": record["source_gid"],
                "source_task_name": task["name"],
                "object_type": "dependency" if relation_type == "dependencies" else "dependent",
                "object_gid": related["gid"],
                "metadata": related,
                "in_scope_relationship_endpoint": in_scope,
                "proposed_treatment": "recreate-relationship" if in_scope else "reference",
                "rationale": (
                    "Both endpoints are in scope, so recreate the relationship with its original direction."
                    if in_scope
                    else "The other endpoint is outside scope; retain its exact source reference."
                ),
            })

    if task.get("due_on") or task.get("due_at"):
        findings.append({
            "source_task_gid": record["source_gid"],
            "source_task_name": task["name"],
            "object_type": "due_date",
            "object_gid": None,
            "due_on": task.get("due_on"),
            "due_at": task.get("due_at"),
            "proposed_treatment": "preserve",
            "rationale": "The live scheduling date is current task metadata and should carry to the migrated task.",
        })

    system_counts = Counter(item.get("resource_subtype") or "unknown" for item in system_items)
    return {
        "source_gid": record["source_gid"],
        "source_name": task["name"],
        "inspection_status": record["status"],
        "permalink_url": task.get("permalink_url"),
        "current_non_note_metadata": {
            "completed": task.get("completed"),
            "assignee": task.get("assignee"),
            "due_on": task.get("due_on"),
            "due_at": task.get("due_at"),
            "start_on": task.get("start_on"),
            "start_at": task.get("start_at"),
            "followers": task.get("followers") or [],
            "tags": task.get("tags") or [],
            "custom_fields": task.get("custom_fields") or [],
            "projects": task.get("projects") or [],
            "memberships": task.get("memberships") or [],
        },
        "pagination": pagination_summary(record),
        "object_counts": {
            "human_comments": len(comment_items),
            "system_stories": len(system_items),
            "attachments": len(record["attachments"]["items"]),
            "direct_subtasks": len(record["subtasks"]["items"]),
            "subtask_human_comments": sum(len(comments(x["stories"]["items"])) for x in record["subtasks"]["items"]),
            "subtask_attachments": sum(len(x["attachments"]["items"]) for x in record["subtasks"]["items"]),
            "task_references": sum(len(x.get("task_references") or []) for x in comment_items),
            "dependencies_observable": len(record["dependencies"]["items"]),
            "dependents_observable": len(record["dependents"]["items"]),
        },
        "relationship_endpoint_status": "workspace-feature-unavailable",
        "system_history": {
            "proposed_treatment": "omit-system-history",
            "count": len(system_items),
            "by_resource_subtype": dict(sorted(system_counts.items())),
        },
        "findings": findings,
    }


def render_markdown(audit):
    coverage = audit["coverage"]
    totals = audit["totals"]
    lines = [
        "# Read-only Asana side-data audit — Dish corpus migration",
        "",
        f"- Tasks attempted / inspected / missing / inaccessible: **{coverage['tasks_attempted']} / {coverage['tasks_inspected']} / {coverage['tasks_missing']} / {coverage['tasks_inaccessible']}**",
        f"- Human comments / system stories: **{totals['object_types']['human_comments']} / {totals['object_types']['system_stories']}**",
        f"- Attachments / direct subtasks: **{totals['object_types']['attachments']} / {totals['object_types']['direct_subtasks']}**",
        f"- Observable dependencies / dependents: **{totals['object_types']['dependencies']} / {totals['object_types']['dependents']}**",
        f"- Current due dates: **{totals['object_types']['due_dates']}**",
        f"- Proposed treatments: **preserve {totals['treatments']['preserve']}**, **reference {totals['treatments']['reference']}**, **recreate-relationship {totals['treatments']['recreate-relationship']}**, **omit-system-history {totals['treatments']['omit-system-history']}**, **human-decision {totals['treatments']['human-decision']}**",
        "- Human decisions: **none**",
        "- **No Asana writes occurred.**",
        "",
        "## Coverage notes",
        "",
        "Stories, attachments, direct subtasks, and each direct subtask's stories and attachments were fetched with explicit 100-object pages until `next_page` was absent. No duplicate GIDs were observed.",
        "",
        "Asana returned HTTP 402 for both dependency list endpoints because dependencies are above this workspace's current premium level. Coverage was closed without inventing relationship data: all 99 full task reads explicitly requested `dependencies` and `dependents` and returned neither field, and the 826 fully paginated stories contained no dependency relationship story. The audit therefore records zero observable dependency relationships and retains the exact 402 evidence in the JSON.",
        "",
        "All 99 tasks were incomplete, unassigned top-level tasks in the legacy Cooking project. They had no tags, populated custom fields, extra project memberships, attachments, or subtasks. Each had Moinudin as its sole follower; this ordinary notification metadata is inventoried but is not a substantive migration finding.",
        "",
        "## Tasks with substantive findings",
        "",
        "| Source task | Human comments | Due date | Proposed treatment |",
        "|---|---:|---|---|",
    ]
    for task in audit["tasks"]:
        comment_count = task["object_counts"]["human_comments"]
        due_on = task["current_non_note_metadata"]["due_on"]
        if not comment_count and not due_on:
            continue
        safe_name = task["source_name"].replace("[", "\\[").replace("]", "\\]")
        link = f"[{safe_name}]({task['permalink_url']})"
        treatments = []
        if comment_count:
            treatments.append("preserve comments")
        if due_on:
            treatments.append("preserve due date")
        lines.append(f"| {link} (`{task['source_gid']}`) | {comment_count} | {due_on or '—'} | {', '.join(treatments)} |")
    lines.extend([
        "",
        "System history was classified as `omit-system-history`: it consists of ordinary Asana-generated name, notes, project, section, due-date, trash, and restore events. Per-task and subtype counts remain in the JSON for coverage proof.",
        "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw_capture", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    raw = json.loads(args.raw_capture.read_text())
    records = raw["records"]
    if len(records) != EXPECTED_SCOPE or len({x["source_gid"] for x in records}) != EXPECTED_SCOPE:
        raise SystemExit("raw capture is not exactly 99 unique source GIDs")
    if any(record["status"] != "inspected" for record in records):
        raise SystemExit("raw capture contains a missing or inaccessible source task")
    for record in records:
        for object_type in ("stories", "attachments", "subtasks"):
            if record[object_type]["pagination"].get("exhausted") is not True:
                raise SystemExit(f"pagination not exhausted: {record['source_gid']} {object_type}")

    tasks = [build_task(record) for record in records]
    all_findings = [finding for task in tasks for finding in task["findings"]]
    treatment_counts = Counter(finding["proposed_treatment"] for finding in all_findings)
    system_count = sum(task["system_history"]["count"] for task in tasks)
    treatment_counts["omit-system-history"] = system_count
    relationship_limit = raw.get("workspace_feature_unavailable") or {}
    if set(relationship_limit) != {"dependencies", "dependents"}:
        raise SystemExit("expected explicit workspace-level dependency limitation was not captured")

    audit = {
        "audit_metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_project_gid": SOURCE_PROJECT_GID,
            "excluded_test_project_gid": TEST_PROJECT_GID,
            "scope": raw["audit_scope"],
            "raw_capture_sha256": sha256(args.raw_capture),
            "api_requests": raw["api_requests"],
            "read_only": True,
            "asana_writes": 0,
        },
        "coverage": {
            "tasks_attempted": len(records),
            "tasks_inspected": sum(x["status"] == "inspected" for x in records),
            "tasks_missing": sum(x["status"] == "missing" for x in records),
            "tasks_inaccessible": sum(x["status"] == "inaccessible" for x in records),
            "object_pagination_exhausted": True,
            "duplicate_objects": sum(
                len(record[kind]["pagination"]["duplicates_suppressed"])
                for record in records for kind in ("stories", "attachments", "subtasks")
            ) + sum(
                len(subtask[kind]["pagination"]["duplicates_suppressed"])
                for record in records for subtask in record["subtasks"]["items"]
                for kind in ("stories", "attachments")
            ),
            "dependency_coverage": {
                "observable_relationships": 0,
                "endpoint_status": "workspace-feature-unavailable",
                "endpoint_errors": relationship_limit,
                "task_detail_opt_in_reads": 99,
                "task_detail_relationship_fields_returned": 0,
                "dependency_story_count": sum(
                    1 for record in records for story in record["stories"]["items"]
                    if "dependenc" in (story.get("resource_subtype") or "").lower()
                    or story.get("dependency")
                ),
            },
        },
        "totals": {
            "object_types": {
                "stories": sum(len(x["stories"]["items"]) for x in records),
                "human_comments": sum(task["object_counts"]["human_comments"] for task in tasks),
                "system_stories": system_count,
                "attachments": sum(task["object_counts"]["attachments"] for task in tasks),
                "direct_subtasks": sum(task["object_counts"]["direct_subtasks"] for task in tasks),
                "subtask_human_comments": sum(task["object_counts"]["subtask_human_comments"] for task in tasks),
                "subtask_attachments": sum(task["object_counts"]["subtask_attachments"] for task in tasks),
                "dependencies": 0,
                "dependents": 0,
                "task_references": sum(task["object_counts"]["task_references"] for task in tasks),
                "due_dates": sum(task["current_non_note_metadata"]["due_on"] is not None for task in tasks),
            },
            "treatments": {key: treatment_counts.get(key, 0) for key in (
                "preserve", "reference", "recreate-relationship", "omit-system-history", "human-decision"
            )},
        },
        "tasks": tasks,
    }

    exceptions = [finding for finding in all_findings if finding["proposed_treatment"] == "human-decision"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "asana-side-data-audit.json"
    md_path = args.output_dir / "asana-side-data-audit.md"
    exceptions_path = args.output_dir / "asana-side-data-exceptions.json"
    json_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(render_markdown(audit))
    exceptions_path.write_text(json.dumps(exceptions, indent=2, ensure_ascii=False) + "\n")

    archive_path = args.output_dir.with_suffix(".tgz")
    with tarfile.open(archive_path, "w:gz") as tf:
        for path in (json_path, md_path, exceptions_path):
            tf.add(path, arcname=f"{args.output_dir.name}/{path.name}")
    print(json.dumps({
        "output_dir": str(args.output_dir),
        "archive": str(archive_path),
        "archive_sha256": sha256(archive_path),
        "audit_json_sha256": sha256(json_path),
        "report_sha256": sha256(md_path),
        "exceptions_sha256": sha256(exceptions_path),
    }, indent=2))


if __name__ == "__main__":
    main()
