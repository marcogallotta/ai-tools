#!/usr/bin/env python3
"""Deterministic, read-only exporter for the corpus-migration source capture.

For each released governed task GID in the eligible-88 manifest, calls the
existing `tools/asana` CLI's read-only `raw GET` command against the live
Asana API, then records exact name/notes bytes, section placement, in-section
order, `modified_at`, and a SHA-256 of the exact UTF-8 notes bytes. This
script performs no Asana write of any kind.

A row is marked `source_capture_status: captured` only when the API call
succeeds and the captured name and section match the eligible-88 manifest's
expected values exactly. Any mismatch or error is recorded as
`capture-needs-review` with the disagreement noted, and is never silently
promoted to `captured`.

Usage:
    export_corpus_capture.py --eligible-json PATH --out-dir PATH
                              [--asana-bin PATH] [--project-gid GID]
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_ASANA_BIN = Path(__file__).resolve().parents[2] / "tools" / "asana"
DEFAULT_PROJECT_GID = "1215089183018968"  # legacy Asana Cooking (docs/corpus-migration-status.md)

TASK_OPT_FIELDS = "name,notes,modified_at,memberships.section.gid,memberships.section.name,memberships.project.gid"


def run_asana_raw(asana_bin: Path, method: str, path: str) -> dict:
    proc = subprocess.run(
        [str(asana_bin), "raw", method, path],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"asana raw {method} {path} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def fetch_task(asana_bin: Path, gid: str) -> dict:
    return run_asana_raw(asana_bin, "GET", f"/tasks/{gid}?opt_fields={TASK_OPT_FIELDS}")


# tools/asana `raw` unwraps the top-level "data" envelope and does not expose
# pagination metadata, so this only reads the first page. That's sufficient
# here: every section in this 110-task corpus holds well under 100 tasks.
def fetch_section_order(asana_bin: Path, section_gid: str, cache: dict) -> list:
    if section_gid in cache:
        return cache[section_gid]
    result = run_asana_raw(asana_bin, "GET", f"/sections/{section_gid}/tasks?opt_fields=gid&limit=100")
    ordered_gids = [t["gid"] for t in result] if isinstance(result, list) else []
    cache[section_gid] = ordered_gids
    return ordered_gids


def select_membership(task: dict, project_gid: str):
    for m in task.get("memberships") or []:
        project = m.get("project") or {}
        if project.get("gid") == project_gid:
            return m.get("section") or {}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--eligible-json", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--asana-bin", type=Path, default=DEFAULT_ASANA_BIN)
    ap.add_argument("--project-gid", default=DEFAULT_PROJECT_GID)
    args = ap.parse_args()

    if not args.asana_bin.exists():
        sys.exit(f"asana CLI not found at {args.asana_bin}")

    eligible = json.loads(args.eligible_json.read_text(encoding="utf-8"))

    notes_dir = args.out_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    section_order_cache: dict = {}
    rows = []
    failures = []

    for row in eligible:
        gid = row["source_gid"]
        expected_name = row["source_name"]
        expected_section = row["source_section_name"]

        try:
            task = fetch_task(args.asana_bin, gid)
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            failures.append({"source_gid": gid, "error": str(exc)})
            rows.append({
                **row,
                "source_capture_status": "capture-needs-review",
                "capture_error": str(exc),
            })
            continue

        captured_name = task.get("name") or ""
        captured_notes = task.get("notes") or ""
        modified_at = task.get("modified_at")
        section = select_membership(task, args.project_gid)
        section_gid = section.get("gid")
        section_name = section.get("name")

        order_index = None
        if section_gid:
            try:
                ordered_gids = fetch_section_order(args.asana_bin, section_gid, section_order_cache)
                if gid in ordered_gids:
                    order_index = ordered_gids.index(gid)
            except Exception as exc:  # noqa: BLE001
                failures.append({"source_gid": gid, "error": f"section order: {exc}"})

        notes_bytes = captured_notes.encode("utf-8")
        notes_sha256 = hashlib.sha256(notes_bytes).hexdigest()
        notes_path = notes_dir / f"{gid}.txt"
        notes_path.write_bytes(notes_bytes)

        name_matches = captured_name == expected_name
        section_matches = section_name == expected_section

        status = "captured" if (name_matches and section_matches and modified_at) else "capture-needs-review"

        rows.append({
            "source_gid": gid,
            "source_order": row["source_order"],
            "expected_name": expected_name,
            "captured_name": captured_name,
            "name_matches": name_matches,
            "expected_section_name": expected_section,
            "captured_section_name": section_name,
            "captured_section_gid": section_gid,
            "section_matches": section_matches,
            "order_index": order_index,
            "modified_at": modified_at,
            "notes_file": str(notes_path.relative_to(args.out_dir)),
            "notes_sha256": notes_sha256,
            "source_capture_status": status,
        })

    manifest_path = args.out_dir / "capture-manifest.json"
    manifest_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    captured = sum(1 for r in rows if r["source_capture_status"] == "captured")
    needs_review = len(rows) - captured

    print(f"captured: {captured}/{len(rows)}")
    if needs_review:
        print(f"needs review: {needs_review}")
        for r in rows:
            if r["source_capture_status"] != "captured":
                print(f"  {r['source_gid']}: {r.get('capture_error') or 'name/section/modified_at mismatch'}")
    if failures:
        print(f"errors: {len(failures)}", file=sys.stderr)

    return 0 if not needs_review else 1


if __name__ == "__main__":
    sys.exit(main())
