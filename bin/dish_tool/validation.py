"""Manifest and note validation for the guarded dish workflow."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Sequence

from .constants import MANIFEST_FILENAMES
from .errors import ReleaseResolutionError
from .models import ValidationResult

_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 /_-]*):(?:[ \t]*(.*))$")
_TAG_AT_START_RE = re.compile(r"\A\s*\[([^\]]+)\]")


def validate_manifest_shape(
    manifest: Any, *, expected_kind: str, filename: str
) -> dict[str, Any]:
    if expected_kind not in MANIFEST_FILENAMES:
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} has an unknown manifest_kind"
        )
    if not isinstance(manifest, dict):
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} must contain a JSON object"
        )
    if manifest.get("manifest_kind") != expected_kind:
        raise ReleaseResolutionError(
            "manifest_malformed",
            f"{filename} has the wrong manifest_kind",
            expected=expected_kind,
        )
    for category in ("headings", "labels"):
        spec = manifest.get(category)
        if not isinstance(spec, dict):
            raise ReleaseResolutionError(
                "manifest_malformed", f"{filename} is missing {category} rules"
            )
        for key in ("required", "optional", "exactly_once", "allowed"):
            values = spec.get(key)
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ReleaseResolutionError(
                    "manifest_malformed",
                    f"{filename} {category}.{key} must be a list of strings",
                )
            if len(values) != len(set(values)):
                raise ReleaseResolutionError(
                    "manifest_malformed",
                    f"{filename} {category}.{key} contains duplicates",
                )
        allowed = set(spec["allowed"])
        required = set(spec["required"])
        exact_once = set(spec["exactly_once"])
        declared = required | set(spec["optional"]) | exact_once
        if not declared <= allowed:
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} {category} rules name undeclared allowed values",
            )
        if not exact_once <= required:
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} {category}.exactly_once must be required",
            )

    contextual = manifest.get("contextual_labels")
    if not isinstance(contextual, list):
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} contextual_labels must be a list"
        )
    for item in contextual:
        if not isinstance(item, dict) or set(item) != {
            "heading",
            "required_label",
        }:
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} has a malformed contextual label rule",
            )
        if item["heading"] not in manifest["headings"]["allowed"]:
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} contextual heading is not allowed",
            )
        if item["required_label"] not in manifest["labels"]["allowed"]:
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} contextual label is not allowed",
            )

    exemptions = manifest.get("exemptions")
    if not isinstance(exemptions, dict):
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} is missing exemptions grammar"
        )
    if set(exemptions) != {"label", "none_value", "allowed_tags"}:
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} exemptions grammar is malformed"
        )
    if exemptions["label"] not in manifest["labels"]["allowed"]:
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} exemptions label is not allowed"
        )
    tags = exemptions["allowed_tags"]
    if (
        not isinstance(tags, list)
        or not tags
        or not all(isinstance(tag, str) and tag for tag in tags)
    ):
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} allowed_tags is malformed"
        )
    if len(tags) != len(set(tags)):
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} allowed_tags contains duplicates"
        )

    destination = manifest.get("destination_section")
    if not isinstance(destination, dict) or set(destination) != {"label", "pattern"}:
        raise ReleaseResolutionError(
            "manifest_malformed",
            f"{filename} destination_section grammar is malformed",
        )
    if destination["label"] not in manifest["labels"]["allowed"]:
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} destination label is not allowed"
        )
    try:
        regex = re.compile(destination["pattern"])
    except (TypeError, re.error) as exc:
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} destination regex is invalid"
        ) from exc
    if not {"name", "gid"} <= set(regex.groupindex):
        raise ReleaseResolutionError(
            "manifest_malformed",
            f"{filename} destination regex requires name and gid groups",
        )
    return copy.deepcopy(manifest)


def _error(rule: str, **fields: Any) -> dict[str, Any]:
    return {"rule": rule, **fields}


def _extract_structure(
    note: str,
) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    headings: list[str] = []
    labels: dict[str, list[str]] = {}
    labels_by_heading: dict[str, list[str]] = {}
    current_heading = ""
    for raw_line in note.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("#"):
            headings.append(line)
            current_heading = line
            continue
        match = _LABEL_RE.fullmatch(line)
        if match:
            label, value = match.groups()
            labels.setdefault(label, []).append(value)
            labels_by_heading.setdefault(current_heading, []).append(label)
    return headings, labels, labels_by_heading


def _parse_exemptions(
    values: Sequence[str], grammar: Mapping[str, Any]
) -> tuple[tuple[str, ...] | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    if not values:
        return None, errors
    none_value = grammar["none_value"]
    if len(values) > 1:
        has_none = any(value.strip() == none_value for value in values)
        has_tags = any("[" in value for value in values)
        if has_none and has_tags:
            errors.append(_error("mixed_exemptions", field=grammar["label"]))
    value = values[0].strip()
    if value == none_value:
        return (), errors
    if none_value in value:
        errors.append(_error("mixed_exemptions", field=grammar["label"]))

    allowed = set(grammar["allowed_tags"])
    tags: list[str] = []
    remainder = value
    while True:
        match = _TAG_AT_START_RE.match(remainder)
        if not match:
            break
        tag = match.group(1).strip()
        tags.append(tag)
        remainder = remainder[match.end() :]
    if not tags:
        errors.append(_error("invalid_exemptions", field=grammar["label"]))
        return None, errors
    unknown = sorted(set(tags) - allowed)
    if unknown:
        errors.append(
            _error("unknown_exemption_tag", field=grammar["label"], tags=unknown)
        )
    duplicates = sorted({tag for tag in tags if tags.count(tag) > 1})
    if duplicates:
        errors.append(
            _error("duplicate_exemption_tag", field=grammar["label"], tags=duplicates)
        )
    if not remainder.strip():
        errors.append(_error("missing_exemption_explanation", field=grammar["label"]))
    return tuple(sorted(set(tags))), errors

def extract_exact_label_line(note: str, label: str) -> str | None:
    """Return the one literal label line, preserving its value and spacing."""

    matches: list[str] = []
    for raw_line in note.splitlines():
        line = raw_line.rstrip("\r")
        match = _LABEL_RE.fullmatch(line)
        if match and match.group(1) == label:
            matches.append(line)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"expected one {label} line, found {len(matches)}")
    return matches[0]

def validate_note(note: str, manifest: Mapping[str, Any]) -> ValidationResult:
    """Validate only literal shape and the two narrow operational grammars."""

    # A release resolver already validates manifests, but commands may load a
    # frozen JSON value from SQLite, so validate its shape again before trusting it.
    checked = validate_manifest_shape(
        manifest,
        expected_kind=str(manifest.get("manifest_kind", "")),
        filename="frozen canonical manifest",
    )
    headings, labels, labels_by_heading = _extract_structure(note)
    errors: list[dict[str, Any]] = []

    for category, found in (("heading", headings), ("label", labels)):
        spec = checked[f"{category}s"]
        counts = (
            {value: found.count(value) for value in set(found)}
            if category == "heading"
            else {value: len(found.get(value, [])) for value in found}
        )
        for value in spec["required"]:
            if counts.get(value, 0) == 0:
                errors.append(_error(f"missing_{category}", field=value))
        for value in spec["exactly_once"]:
            count = counts.get(value, 0)
            if count > 1:
                errors.append(_error(f"duplicate_{category}", field=value, count=count))
        allowed = set(spec["allowed"])
        for value in counts:
            if value not in allowed:
                errors.append(_error(f"unknown_{category}", field=value))

    for rule in checked["contextual_labels"]:
        heading = rule["heading"]
        label = rule["required_label"]
        if heading in headings and label not in labels_by_heading.get(heading, []):
            errors.append(
                _error(
                    "missing_contextual_label",
                    heading=heading,
                    field=label,
                )
            )

    exemption_label = checked["exemptions"]["label"]
    exemption_tags, exemption_errors = _parse_exemptions(
        labels.get(exemption_label, []), checked["exemptions"]
    )
    errors.extend(exemption_errors)

    destination_name = None
    destination_gid = None
    destination_label = checked["destination_section"]["label"]
    destination_values = labels.get(destination_label, [])
    if len(destination_values) == 1:
        destination_match = re.fullmatch(
            checked["destination_section"]["pattern"], destination_values[0].strip()
        )
        if destination_match is None:
            errors.append(_error("invalid_destination", field=destination_label))
        else:
            destination_name = destination_match.group("name").strip()
            destination_gid = destination_match.group("gid").strip()
            if not destination_name or not destination_gid:
                errors.append(_error("invalid_destination", field=destination_label))

    return ValidationResult(
        errors=tuple(errors),
        exemption_tags=exemption_tags,
        destination_name=destination_name,
        destination_gid=destination_gid,
    )
