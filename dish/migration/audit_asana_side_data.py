#!/usr/bin/env python3
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import asana
from asana.rest import ApiException


ARCHIVE = Path("/home/marco/Downloads/batch-002-correction-4-codex-verified.tgz")
MANIFEST_MEMBER = "dish_migration_batch_002_correction_4/manifest-batch-002.json"
OUT = Path("/tmp/asana-side-data-audit-raw.json")
PAGE_LIMIT = 100
ASANA_TASK_URL = re.compile(r"https://app\.asana\.com/[^\s\"'<>]+")
TASK_PATH = re.compile(r"/task/(\d+)")
CLASSIC_TASK_PATH = re.compile(r"/0/\d+/(\d+)(?:/|$)")


def load_pat():
    pat = os.environ.get("ASANA_PAT")
    if pat:
        return pat
    env_path = Path(os.environ.get("ASANA_ENV", "~/.config/asana-cli/.env")).expanduser()
    for line in env_path.read_text().splitlines():
        if line.strip().startswith("ASANA_PAT="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("ASANA_PAT not found")


class AsanaReader:
    def __init__(self):
        config = asana.Configuration()
        config.access_token = load_pat()
        config.return_page_iterator = False
        self.client = asana.ApiClient(config)
        self.requests = 0

    def close(self):
        pool = getattr(self.client, "pool", None)
        if pool is not None:
            pool.close()
            pool.join()

    def get(self, path, params=None):
        query = urllib.parse.urlencode(params or {}, safe=",")
        target = path + (("&" if "?" in path else "?") + query if query else "")
        for attempt in range(6):
            try:
                self.requests += 1
                return self.client.call_api(
                    target,
                    "GET",
                    header_params={"Accept": "application/json; charset=UTF-8"},
                    response_type=object,
                    auth_settings=["personalAccessToken"],
                    _return_http_data_only=True,
                )
            except ApiException as exc:
                if exc.status == 429 or (exc.status is not None and exc.status >= 500):
                    if attempt < 5:
                        time.sleep(min(2 ** attempt, 16))
                        continue
                raise

    def pages(self, path, opt_fields):
        items = []
        offsets = []
        seen_offsets = set()
        seen_gids = set()
        duplicates = []
        offset = None
        page_count = 0
        while True:
            params = {"limit": PAGE_LIMIT, "opt_fields": opt_fields}
            if offset:
                params["offset"] = offset
            result = self.get(path, params)
            page_count += 1
            for item in result.get("data") or []:
                gid = item.get("gid")
                if gid and gid in seen_gids:
                    duplicates.append(gid)
                    continue
                if gid:
                    seen_gids.add(gid)
                items.append(item)
            next_page = result.get("next_page") or {}
            next_offset = next_page.get("offset")
            if not next_offset:
                return {
                    "items": items,
                    "pagination": {
                        "page_count": page_count,
                        "exhausted": True,
                        "offsets_followed": offsets,
                        "duplicates_suppressed": duplicates,
                    },
                }
            if next_offset in seen_offsets:
                raise RuntimeError(f"pagination offset repeated for {path}")
            seen_offsets.add(next_offset)
            offsets.append(next_offset)
            offset = next_offset


def error_record(exc):
    body = exc.body.decode(errors="replace") if isinstance(exc.body, bytes) else exc.body
    return {"status": exc.status, "reason": exc.reason, "body": str(body or "")[:1000]}


def task_refs(*texts):
    found = []
    seen = set()
    for value in texts:
        for raw_url in ASANA_TASK_URL.findall(html.unescape(value or "")):
            url = raw_url.rstrip(".,);]")
            match = TASK_PATH.search(url) or CLASSIC_TASK_PATH.search(url)
            key = (url, match.group(1) if match else None)
            if key not in seen:
                seen.add(key)
                found.append({"url": url, "task_gid": key[1]})
    return found


def main():
    import tarfile

    expected_sha = "c3a2ce255fc50f2085e3bb9c03b658061bfbbfef4daf3ec6325296fc6454505f"
    actual_sha = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(f"archive SHA mismatch: {actual_sha}")
    with tarfile.open(ARCHIVE, "r:gz") as tf:
        manifest = json.load(tf.extractfile(MANIFEST_MEMBER))
    if len(manifest) != 99 or len({x["source_gid"] for x in manifest}) != 99:
        raise RuntimeError("manifest scope is not exactly 99 unique source GIDs")
    in_scope = {x["source_gid"] for x in manifest}

    reader = AsanaReader()
    records = []
    unavailable_relationship_endpoints = {}
    try:
        for index, entry in enumerate(manifest, 1):
            gid = entry["source_gid"]
            rec = {
                "source_gid": gid,
                "manifest_source_name": entry["source_name"],
                "status": "inspected",
                "errors": [],
            }
            try:
                detail = reader.get(
                    f"/tasks/{gid}",
                    {"opt_fields": ",".join([
                        "gid", "name", "permalink_url", "completed", "created_at", "modified_at",
                        "resource_subtype", "parent.gid", "parent.name", "assignee.gid", "assignee.name",
                        "due_on", "due_at", "start_on", "start_at", "approval_status",
                        "memberships.project.gid", "memberships.project.name",
                        "memberships.section.gid", "memberships.section.name",
                        "projects.gid", "projects.name", "tags.gid", "tags.name",
                        "custom_fields.gid", "custom_fields.name", "custom_fields.resource_subtype",
                        "custom_fields.display_value", "followers.gid", "followers.name",
                        "num_subtasks", "dependencies.gid", "dependencies.name",
                        "dependents.gid", "dependents.name",
                    ])},
                ).get("data")
                rec["task"] = detail
            except ApiException as exc:
                rec["status"] = "missing" if exc.status == 404 else "inaccessible"
                rec["errors"].append({"object_type": "task", **error_record(exc)})
                records.append(rec)
                print(f"[{index:02d}/99] {gid} {rec['status']}", flush=True)
                continue

            endpoints = {
                "stories": (
                    f"/tasks/{gid}/stories",
                    "gid,created_at,created_by.gid,created_by.name,resource_subtype,text,html_text,type,dependency.gid,dependency.name,task.gid,task.name",
                ),
                "attachments": (
                    "/attachments",
                    "gid,name,resource_subtype,host,created_at,download_url,permanent_url,view_url,parent.gid",
                ),
                "subtasks": (
                    f"/tasks/{gid}/subtasks",
                    "gid,name,completed,created_at,modified_at,permalink_url,parent.gid,assignee.gid,assignee.name,due_on,due_at,start_on,start_at,tags.gid,tags.name,custom_fields.gid,custom_fields.name,custom_fields.display_value",
                ),
                "dependencies": (
                    f"/tasks/{gid}/dependencies", "gid,name,permalink_url"
                ),
                "dependents": (
                    f"/tasks/{gid}/dependents", "gid,name,permalink_url"
                ),
            }
            for kind, (path, fields) in endpoints.items():
                if kind in unavailable_relationship_endpoints:
                    rec[kind] = {
                        "items": [],
                        "pagination": {"exhausted": False},
                        "error": unavailable_relationship_endpoints[kind],
                        "workspace_feature_unavailable": True,
                    }
                    continue
                try:
                    if kind == "attachments":
                        path = f"{path}?parent={gid}"
                    rec[kind] = reader.pages(path, fields)
                except ApiException as exc:
                    rec[kind] = {"items": [], "pagination": {"exhausted": False}, "error": error_record(exc)}
                    rec["errors"].append({"object_type": kind, **error_record(exc)})
                    if kind in ("dependencies", "dependents") and exc.status == 402:
                        unavailable_relationship_endpoints[kind] = error_record(exc)

            for subtask in rec.get("subtasks", {}).get("items", []):
                subtask["in_scope_source_gid"] = subtask.get("gid") in in_scope
                try:
                    subtask["stories"] = reader.pages(
                        f"/tasks/{subtask['gid']}/stories",
                        "gid,created_at,created_by.gid,created_by.name,resource_subtype,text,html_text,type,dependency.gid,dependency.name,task.gid,task.name",
                    )
                except ApiException as exc:
                    subtask["stories"] = {"items": [], "pagination": {"exhausted": False}, "error": error_record(exc)}
                    rec["errors"].append({"object_type": "subtask_stories", "subtask_gid": subtask["gid"], **error_record(exc)})
                try:
                    subtask["attachments"] = reader.pages(
                        f"/attachments?parent={subtask['gid']}",
                        "gid,name,resource_subtype,host,created_at,download_url,permanent_url,view_url,parent.gid",
                    )
                except ApiException as exc:
                    subtask["attachments"] = {"items": [], "pagination": {"exhausted": False}, "error": error_record(exc)}
                    rec["errors"].append({"object_type": "subtask_attachments", "subtask_gid": subtask["gid"], **error_record(exc)})
            for kind in ("dependencies", "dependents"):
                for related in rec.get(kind, {}).get("items", []):
                    related["in_scope_source_gid"] = related.get("gid") in in_scope
            for story in rec.get("stories", {}).get("items", []):
                story["task_references"] = task_refs(story.get("text"), story.get("html_text"))
                for ref in story["task_references"]:
                    ref["in_scope_source_gid"] = ref.get("task_gid") in in_scope if ref.get("task_gid") else None
            records.append(rec)
            print(
                f"[{index:02d}/99] {gid} stories={len(rec.get('stories', {}).get('items', []))} "
                f"attachments={len(rec.get('attachments', {}).get('items', []))} "
                f"subtasks={len(rec.get('subtasks', {}).get('items', []))}",
                flush=True,
            )
    finally:
        reader.close()

    payload = {
        "audit_scope": {
            "archive": str(ARCHIVE),
            "archive_sha256": actual_sha,
            "manifest_member": MANIFEST_MEMBER,
            "source_gid_count": len(manifest),
        },
        "api_requests": reader.requests,
        "workspace_feature_unavailable": unavailable_relationship_endpoints,
        "records": records,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({reader.requests} API requests)")


if __name__ == "__main__":
    main()
