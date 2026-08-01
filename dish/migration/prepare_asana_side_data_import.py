#!/usr/bin/env python3
"""Prepare or verify the guarded Asana batch for migrated comments and due dates.

This script is read-only against Asana. It validates the complete source-to-target
mapping and exact target task baselines, then writes a deterministic
``asana batch-apply`` plan. A rerun after application must produce zero operations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from audit_asana_side_data import AsanaReader


TEST_PROJECT_GID = "1216693403164366"
IMPORT_MARKER = "DISH LEGACY COMMENTS IMPORT v1"
EXPECTED_TASKS = 99
MAX_COMMENT_CHARACTERS = 25_000


class PreparationFailure(RuntimeError):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PreparationFailure(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PreparationFailure(f"invalid JSON in {path}: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PreparationFailure(f"{label} must be a JSON object")
    return value


def comment_payload(task: Mapping[str, Any]) -> dict[str, Any]:
    comments = []
    for finding in task["findings"]:
        if finding.get("object_type") != "human_comment":
            continue
        comments.append({
            "story_gid": finding["object_gid"],
            "created_at": finding.get("created_at"),
            "author": finding.get("author"),
            "text": finding.get("text") or "",
            "html_text": finding.get("html_text") or "",
            "task_references": finding.get("task_references") or [],
        })
    comments.sort(key=lambda item: (item["created_at"] or "", item["story_gid"]))
    return {
        "source_task_gid": task["source_gid"],
        "source_task_name": task["source_name"],
        "source_task_url": task["permalink_url"],
        "comments": comments,
    }


def render_comment_block(task: Mapping[str, Any]) -> str | None:
    payload = comment_payload(task)
    if not payload["comments"]:
        return None
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = sha256_bytes(canonical)
    displayed = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    text = "\n".join((
        IMPORT_MARKER,
        f"Source task GID: {task['source_gid']}",
        f"Payload SHA-256: {digest}",
        "Original author, timestamp, story GID, plain text, HTML text, and task references follow.",
        displayed,
    ))
    if len(text) > MAX_COMMENT_CHARACTERS:
        raise PreparationFailure(
            f"{task['source_gid']}: legacy comment block is {len(text)} characters; "
            f"limit is {MAX_COMMENT_CHARACTERS}"
        )
    return text


def target_marker(source_gid: str) -> str:
    return f"{IMPORT_MARKER}\nSource task GID: {source_gid}\n"


def validate_scope(audit: Mapping[str, Any], mapping: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    tasks = audit.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != EXPECTED_TASKS:
        raise PreparationFailure(f"audit must contain exactly {EXPECTED_TASKS} tasks")
    by_gid = {}
    for task_value in tasks:
        task = as_mapping(task_value, "audit task")
        gid = str(task.get("source_gid") or "").strip()
        if not gid.isdigit() or gid in by_gid:
            raise PreparationFailure(f"invalid or duplicate audit source_gid: {gid!r}")
        by_gid[gid] = task
    if set(by_gid) != set(mapping):
        missing = sorted(set(by_gid) - set(mapping))
        extra = sorted(set(mapping) - set(by_gid))
        raise PreparationFailure(f"mapping coverage differs from audit; missing={missing}, extra={extra}")
    target_gids = [str(as_mapping(mapping[gid], f"mapping {gid}").get("target_gid") or "") for gid in by_gid]
    if any(not gid.isdigit() for gid in target_gids) or len(set(target_gids)) != EXPECTED_TASKS:
        raise PreparationFailure("mapping must contain 99 unique numeric target GIDs")
    return by_gid


def prepare(
    *, audit_path: Path, mapping_path: Path, output_path: Path,
) -> dict[str, Any]:
    audit = as_mapping(load_json(audit_path), "audit")
    mapping = as_mapping(load_json(mapping_path), "mapping")
    tasks = validate_scope(audit, mapping)
    reader = AsanaReader()
    operations = []
    checked = []
    try:
        for source_gid in sorted(tasks, key=int):
            source_task = tasks[source_gid]
            target = as_mapping(mapping[source_gid], f"mapping {source_gid}")
            target_gid = str(target["target_gid"])
            detail = reader.get(
                f"/tasks/{target_gid}",
                {"opt_fields": (
                    "gid,name,notes,due_on,due_at,parent.gid,"
                    "memberships.project.gid,memberships.section.gid"
                )},
            )["data"]
            memberships = detail.get("memberships") or []
            project_gids = {str(item["project"]["gid"]) for item in memberships}
            if project_gids != {TEST_PROJECT_GID}:
                raise PreparationFailure(
                    f"{source_gid}: target {target_gid} project memberships are {sorted(project_gids)}"
                )
            if detail.get("parent") is not None:
                raise PreparationFailure(f"{source_gid}: target {target_gid} is not top-level")
            expected_section = str(target["section_gid"])
            section_gids = {
                str(item["section"]["gid"])
                for item in memberships
                if item.get("section") is not None
            }
            if section_gids != {expected_section}:
                raise PreparationFailure(
                    f"{source_gid}: target section {sorted(section_gids)} != {expected_section}"
                )
            notes_hash = sha256_bytes((detail.get("notes") or "").encode("utf-8"))
            if notes_hash != str(target["notes_sha256"]):
                raise PreparationFailure(
                    f"{source_gid}: target notes SHA-256 changed: {notes_hash}"
                )

            expected_comment = render_comment_block(source_task)
            stories = reader.pages(
                f"/tasks/{target_gid}/stories",
                "gid,resource_subtype,text,created_at,created_by.gid,created_by.name",
            )
            existing_marker_comments = [
                story for story in stories["items"]
                if story.get("resource_subtype") == "comment_added"
                and (story.get("text") or "").startswith(target_marker(source_gid))
            ]
            if expected_comment is None:
                if existing_marker_comments:
                    raise PreparationFailure(f"{source_gid}: unexpected legacy-comment marker exists")
                comment_state = "not-applicable"
            elif not existing_marker_comments:
                operations.append({
                    "action": "add_comment",
                    "task": target_gid,
                    "text": expected_comment,
                    "reason": f"Preserve audited legacy human comments from source task {source_gid}.",
                })
                comment_state = "planned"
            elif len(existing_marker_comments) == 1 and existing_marker_comments[0].get("text") == expected_comment:
                comment_state = "confirmed"
            else:
                raise PreparationFailure(
                    f"{source_gid}: legacy-comment marker exists with duplicate or non-exact content"
                )

            expected_due_on = source_task["current_non_note_metadata"].get("due_on")
            expected_due_at = source_task["current_non_note_metadata"].get("due_at")
            if expected_due_at is not None:
                raise PreparationFailure(f"{source_gid}: due_at preservation is not implemented")
            current_due_on = detail.get("due_on")
            if expected_due_on is None:
                if current_due_on is not None:
                    raise PreparationFailure(
                        f"{source_gid}: target has unexpected due_on {current_due_on}"
                    )
                due_state = "not-applicable"
            elif current_due_on is None:
                operations.append({
                    "action": "update_task",
                    "task": target_gid,
                    "field": "due_on",
                    "new": expected_due_on,
                    "reason": f"Preserve audited due date from source task {source_gid}.",
                })
                due_state = "planned"
            elif current_due_on == expected_due_on:
                due_state = "confirmed"
            else:
                raise PreparationFailure(
                    f"{source_gid}: target due_on {current_due_on} conflicts with source {expected_due_on}"
                )
            checked.append({
                "source_gid": source_gid,
                "target_gid": target_gid,
                "comment_state": comment_state,
                "due_date_state": due_state,
            })
    finally:
        reader.close()

    plan = {
        "plan_metadata": {
            "kind": "dish-corpus-side-data-import",
            "environment": "test",
            "target_project_gid": TEST_PROJECT_GID,
            "audit_sha256": sha256_file(audit_path),
            "mapping_sha256": sha256_file(mapping_path),
            "tasks_checked": len(checked),
            "api_requests": reader.requests,
        },
        "operations": operations,
        "verification": checked,
    }
    output_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        plan = prepare(audit_path=args.audit, mapping_path=args.mapping, output_path=args.output)
    except PreparationFailure as exc:
        print(f"FAIL: {exc}")
        return 1
    counts: dict[str, int] = {}
    for operation in plan["operations"]:
        action = operation["action"]
        counts[action] = counts.get(action, 0) + 1
    print(
        f"PASS 99 targets checked; operations={len(plan['operations'])}; "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
