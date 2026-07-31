#!/usr/bin/env python3
"""Deterministic offline validator for transformed Dish migration Batch 002.

Stdlib only. Reads local files; performs no network or Asana access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

STATE_PLACEHOLDER = "{{MIGRATED_DISH_STATE_BLOCK}}"
SECTION_PLACEHOLDER = "{{TARGET_SECTION_GID}}"
PROVENANCE_LABEL = "Legacy agent construction/provenance (not Dish evidence):"
KOREAN_NAMES = {
    "Pajeon",
    "Doenjang-jjigae",
    "Kongnamul-muchim",
    "Kimchi family",
    "Bibimbap",
    "Dubu-jorim",
}


@dataclass(frozen=True, order=True)
class Failure:
    gid: str
    name: str
    rule: int
    reason: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes root: {relative}") from exc
    return candidate


def task_name(capture: dict[str, Any] | None, batch: dict[str, Any] | None, fallback: str) -> str:
    for obj, keys in (
        (capture, ("expected_name", "captured_name")),
        (batch, ("source_name",)),
    ):
        if obj:
            for key in keys:
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return fallback


def one_line(value: str, limit: int = 80) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def extract_markdown_block(text: str, heading: str) -> str | None:
    pattern = re.compile(
        rf"(?m)^### {re.escape(heading)}[ \t]*\r?\n(.*?)(?=^### [^\r\n]+[ \t]*\r?$|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(text)
    return match.group(1) if match else None


def planning_lines(block: str) -> list[str]:
    return [line.strip() for line in block.splitlines() if line.strip()]


def parse_planning(block: str, expected_fields: list[str]) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    lines = planning_lines(block)
    if len(lines) != len(expected_fields):
        errors.append(f"Planning brief has {len(lines)} non-empty lines; expected {len(expected_fields)}")
    for index, field in enumerate(expected_fields):
        if index >= len(lines):
            errors.append(f"missing line {index + 1}: {field}")
            continue
        line = lines[index]
        prefix = field + ":"
        if not line.startswith(prefix):
            errors.append(f"line {index + 1} must begin '{prefix}'")
            continue
        value = line[len(prefix) :].strip()
        if not value:
            errors.append(f"field '{field}' is empty")
        values[field] = value
    if len(lines) > len(expected_fields):
        for extra in lines[len(expected_fields) :]:
            errors.append(f"unexpected Planning brief line: {one_line(extra)}")
    return values, errors


def outside_provenance(text: str) -> str:
    """Remove clearly labeled legacy-provenance blocks for literal evidence checks."""
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == PROVENANCE_LABEL:
            inside = True
            continue
        if inside and re.match(r"^###\s+", stripped):
            inside = False
        if not inside:
            result.append(line)
    return "".join(result)


def normalized_name(value: str) -> str:
    # Used only for the six named Korean tasks; strips bracket tags and trailing descriptors.
    value = re.sub(r"^\[[^\]]+\]\s*", "", value.strip())
    value = re.sub(r"\s+[—-].*$", "", value).strip()
    return value


def iter_explicit_field_values(text: str, label: str) -> Iterable[str]:
    pattern = re.compile(rf"(?m)^{re.escape(label)}:\s*(.*?)\s*$")
    for match in pattern.finditer(text):
        yield match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-in", required=True, type=Path)
    parser.add_argument("--batch-dir", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    args = parser.parse_args()

    failures: list[Failure] = []
    failed_gids: set[str] = set()

    def fail(gid: str, name: str, rule: int, reason: str) -> None:
        failures.append(Failure(str(gid), one_line(name, 70), rule, one_line(reason, 220)))
        if gid != "GLOBAL":
            failed_gids.add(str(gid))

    try:
        capture_raw = load_json(args.manifest_in)
        batch_manifest_path = args.batch_dir / "manifest-batch-002.json"
        batch_raw = load_json(batch_manifest_path)
        schema = load_json(args.schema)
    except ValueError as exc:
        print(f"FAIL GLOBAL input: rule 1 {exc}")
        print("0/99 tasks passed all checks")
        return 2

    if not isinstance(capture_raw, list):
        print("FAIL GLOBAL capture manifest: rule 1 top-level JSON must be an array")
        print("0/99 tasks passed all checks")
        return 2
    if not isinstance(batch_raw, list):
        print("FAIL GLOBAL batch manifest: rule 1 top-level JSON must be an array")
        print(f"0/{len(capture_raw) or 99} tasks passed all checks")
        return 2
    if not isinstance(schema, dict) or not isinstance(schema.get("task_document"), dict):
        print("FAIL GLOBAL schema: rule 7 missing object 'task_document'")
        print(f"0/{len(capture_raw) or 99} tasks passed all checks")
        return 2

    task_schema = schema["task_document"]
    expected_fields = task_schema.get("planning_brief_fields")
    process_subheadings = task_schema.get("process_subheadings")
    allowed_statuses = set(task_schema.get("allowed_statuses", []))
    allowed_rb_classes = set(task_schema.get("research_basis_classifications", []))
    human_prefix = task_schema.get("human_decision_prefix")
    allowed_exemptions = set(
        schema.get("legacy_validation_adapter", {})
        .get("complete_task", {})
        .get("exemptions", {})
        .get("allowed_tags", [])
    )
    if not isinstance(expected_fields, list) or not all(isinstance(x, str) for x in expected_fields):
        print("FAIL GLOBAL schema: rule 7 task_document.planning_brief_fields must be a string array")
        print(f"0/{len(capture_raw) or 99} tasks passed all checks")
        return 2

    capture_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in capture_raw:
        if not isinstance(row, dict) or not str(row.get("source_gid", "")).strip():
            fail("GLOBAL", "capture manifest", 1, "capture row is not an object with source_gid")
            continue
        capture_groups[str(row["source_gid"])].append(row)

    batch_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in batch_raw:
        if not isinstance(row, dict) or not str(row.get("source_gid", "")).strip():
            fail("GLOBAL", "batch manifest", 1, "batch row is not an object with source_gid")
            continue
        batch_groups[str(row["source_gid"])].append(row)

    for gid, rows in sorted(capture_groups.items()):
        if len(rows) != 1:
            fail(gid, task_name(rows[0], None, gid), 1, f"capture manifest contains {len(rows)} rows for source_gid")
    for gid, rows in sorted(batch_groups.items()):
        if len(rows) != 1:
            fail(gid, task_name(None, rows[0], gid), 1, f"batch manifest contains {len(rows)} rows for source_gid")

    capture_by_gid = {gid: rows[0] for gid, rows in capture_groups.items() if rows}
    batch_by_gid = {gid: rows[0] for gid, rows in batch_groups.items() if rows}
    capture_gids = set(capture_by_gid)
    batch_gids = set(batch_by_gid)

    for gid in sorted(capture_gids - batch_gids):
        fail(gid, task_name(capture_by_gid[gid], None, gid), 1, "source_gid missing from batch manifest")
    for gid in sorted(batch_gids - capture_gids):
        fail(gid, task_name(None, batch_by_gid[gid], gid), 1, "source_gid is not present in capture manifest")

    referenced_templates: Counter[Path] = Counter()
    template_for_gid: dict[str, Path] = {}
    for gid, row in sorted(batch_by_gid.items()):
        name = task_name(capture_by_gid.get(gid), row, gid)
        rel = row.get("template_file")
        if not isinstance(rel, str) or not rel.strip():
            fail(gid, name, 1, "batch manifest template_file is missing or empty")
            continue
        try:
            path = safe_child(args.batch_dir, rel)
        except ValueError as exc:
            fail(gid, name, 1, str(exc))
            continue
        referenced_templates[path] += 1
        template_for_gid[gid] = path
        if referenced_templates[path] > 1:
            fail(gid, name, 1, f"template file is referenced more than once: {rel}")
        if not path.is_file():
            fail(gid, name, 1, f"referenced template file does not exist: {rel}")

    templates_dir = args.batch_dir / "templates"
    actual_templates = set(templates_dir.glob("*.md")) if templates_dir.is_dir() else set()
    for orphan in sorted(actual_templates - set(referenced_templates)):
        gid_match = re.match(r"(\d+)", orphan.name)
        gid = gid_match.group(1) if gid_match else "GLOBAL"
        capture = capture_by_gid.get(gid)
        fail(gid, task_name(capture, None, orphan.stem), 1, f"orphan template file not referenced by batch manifest: templates/{orphan.name}")

    capture_root = args.manifest_in.parent

    for gid in sorted(capture_gids & batch_gids):
        capture = capture_by_gid[gid]
        batch = batch_by_gid[gid]
        name = task_name(capture, batch, gid)

        # Rule 2: source fidelity.
        original_rel = capture.get("notes_file")
        batch_rel = batch.get("source_notes_file", f"source-notes/{gid}.txt")
        original_bytes: bytes | None = None
        batch_bytes: bytes | None = None
        if not isinstance(original_rel, str) or not original_rel:
            fail(gid, name, 2, "capture manifest notes_file is missing")
        else:
            try:
                original_path = safe_child(capture_root, original_rel)
                original_bytes = original_path.read_bytes()
            except (ValueError, OSError) as exc:
                fail(gid, name, 2, f"cannot read original capture notes: {exc}")
        if not isinstance(batch_rel, str) or not batch_rel:
            fail(gid, name, 2, "batch manifest source_notes_file is missing")
        else:
            try:
                batch_path = safe_child(args.batch_dir, batch_rel)
                batch_bytes = batch_path.read_bytes()
            except (ValueError, OSError) as exc:
                fail(gid, name, 2, f"cannot read batch source notes: {exc}")
        if original_bytes is not None:
            original_hash = sha256_bytes(original_bytes)
            declared_capture_hash = capture.get("notes_sha256")
            if declared_capture_hash != original_hash:
                fail(gid, name, 2, f"original capture file SHA-256 {original_hash} does not match capture manifest {declared_capture_hash}")
            declared_batch_hash = batch.get("source_notes_sha256")
            if declared_batch_hash != original_hash:
                fail(gid, name, 2, f"batch manifest source_notes_sha256 {declared_batch_hash} does not match original capture {original_hash}")
        if original_bytes is not None and batch_bytes is not None and original_bytes != batch_bytes:
            fail(gid, name, 2, f"batch source notes SHA-256 {sha256_bytes(batch_bytes)} differs from original capture {sha256_bytes(original_bytes)}")

        path = template_for_gid.get(gid)
        if path is None or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            fail(gid, name, 1, f"cannot read template as UTF-8: {exc}")
            continue

        # Optional manifest hash consistency is part of coverage/cross-file identity.
        declared_template_hash = batch.get("template_sha256")
        actual_template_hash = sha256_bytes(path.read_bytes())
        if declared_template_hash and declared_template_hash != actual_template_hash:
            fail(gid, name, 1, f"template SHA-256 {actual_template_hash} does not match batch manifest {declared_template_hash}")

        # Rule 3: placeholders.
        for token, label in ((STATE_PLACEHOLDER, "MIGRATED_DISH_STATE_BLOCK"), (SECTION_PLACEHOLDER, "TARGET_SECTION_GID")):
            count = text.count(token)
            if count != 1:
                reason = "missing or resolved" if count == 0 else f"appears {count} times"
                fail(gid, name, 3, f"placeholder {label} {reason}; expected exactly once")

        # Rules 4 and 5: Planning brief and destination.
        block = extract_markdown_block(text, "Planning brief")
        planning: dict[str, str] = {}
        if block is None:
            fail(gid, name, 5, "missing '### Planning brief' block")
        else:
            planning, planning_errors = parse_planning(block, expected_fields)
            for error in planning_errors:
                fail(gid, name, 5, error)
            role = planning.get("Role", "")
            if role and role != "main" and not role.startswith("non-main"):
                fail(gid, name, 5, "Role must be exactly 'main' or begin with 'non-main'")
            destination = planning.get("Destination section")
            expected_section = capture.get("captured_section_name")
            expected_destination = f"{expected_section} — {SECTION_PLACEHOLDER}"
            if not isinstance(expected_section, str) or not expected_section.strip():
                fail(gid, name, 4, "capture manifest captured_section_name is missing or empty")
            elif destination != expected_destination:
                fail(gid, name, 4, f"Destination section is {destination!r}; expected {expected_destination!r}")

        # Rule 6: no fabricated durable evidence literals outside provenance.
        unprotected = outside_provenance(text)
        for literal in ("Verification —", "Research —"):
            if literal in unprotected:
                fail(gid, name, 6, f"literal {literal!r} appears outside the labeled legacy provenance block")

        # Rule 7: schema-checkable structure and enums.
        if isinstance(process_subheadings, list):
            positions: list[int] = []
            for heading in process_subheadings:
                occurrences = [m.start() for m in re.finditer(rf"(?m)^### {re.escape(str(heading))}\s*$", text)]
                if len(occurrences) != 1:
                    fail(gid, name, 7, f"schema process subheading '### {heading}' occurs {len(occurrences)} times; expected once")
                elif occurrences:
                    positions.append(occurrences[0])
            if len(positions) == len(process_subheadings) and positions != sorted(positions):
                fail(gid, name, 7, "schema process subheadings are not in required order")

        if planning:
            exemptions = planning.get("Exemptions", "")
            if exemptions and exemptions != "None" and allowed_exemptions:
                tags = {part.strip() for part in exemptions.split(",") if part.strip()}
                invalid = sorted(tags - allowed_exemptions)
                if invalid:
                    fail(gid, name, 7, f"Exemptions contains schema-unknown tag(s): {', '.join(invalid)}")

        # Scope Status/Research-basis-classification checks to the live record only: text at or
        # after the unresolved state placeholder. Text above it is preserved legacy note prose
        # (e.g. a prior agent's own "PROCESS RECORD" block quoted verbatim) and is not a live field.
        state_index = text.find(STATE_PLACEHOLDER)
        live_text = text[state_index:] if state_index != -1 else text

        for value in iter_explicit_field_values(live_text, "Status"):
            if value and value not in allowed_statuses:
                fail(gid, name, 7, f"Status value {value!r} is not in schema allowed_statuses")

        for value in iter_explicit_field_values(live_text, "Research basis classification"):
            if value and value not in allowed_rb_classes:
                fail(gid, name, 7, f"Research basis classification {value!r} is not allowed by schema")

        decisions = extract_markdown_block(text, "Decisions")
        if decisions is not None and isinstance(human_prefix, str) and human_prefix:
            for line in decisions.splitlines():
                stripped = line.strip()
                if stripped.startswith("Human —") and not stripped.startswith(human_prefix):
                    fail(gid, name, 7, f"Human decision line does not use schema prefix {human_prefix!r}: {one_line(stripped)}")

        # Rule 8: Korean status guard, cross-referenced by task name.
        expected_name = str(capture.get("expected_name", ""))
        if normalized_name(expected_name) in KOREAN_NAMES:
            korean_text = outside_provenance(text)
            forbidden_patterns = [
                (r"\bverified\b", "claims Verified"),
                (r"\bresearch(?:ed)?\s+(?:is\s+)?complete\b", "claims Research complete"),
                (r"\bverification\s+(?:is\s+)?complete\b", "claims Verification complete"),
                (r"\bverification:\s*(?!not\s+done|pending|none)", "contains affirmative Verification field"),
                (r"\bstage:\s*researched\b", "claims legacy researched stage as current progress"),
                (r"\bstatus:\s*(?:ready|pending-verification|pending-human-review)\b", "implies Research/Verification progress in Status"),
            ]
            for pattern, reason in forbidden_patterns:
                if re.search(pattern, korean_text, flags=re.IGNORECASE):
                    fail(gid, name, 8, reason)

    # Ensure all six Korean names exist exactly once in the capture manifest.
    korean_found = Counter(normalized_name(str(row.get("expected_name", ""))) for row in capture_raw if isinstance(row, dict))
    for korean_name in sorted(KOREAN_NAMES):
        count = korean_found[korean_name]
        if count != 1:
            fail("GLOBAL", korean_name, 8, f"Korean task name resolves to {count} capture rows; expected exactly one")

    for item in sorted(set(failures)):
        print(f"FAIL {item.gid} {item.name}: rule {item.rule} {item.reason}")

    total = len(capture_gids)
    passed = max(0, total - len(failed_gids & capture_gids))
    print(f"{passed}/{total} tasks passed all checks")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
