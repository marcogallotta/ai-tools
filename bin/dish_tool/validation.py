"""Manifest and note validation for the guarded dish workflow."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Sequence

from .constants import MANIFEST_FILENAMES, PROTOCOL_FILENAMES
from .errors import ReleaseResolutionError
from .models import TitleFields, TitleValidationResult, ValidationResult

_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 /_-]*):(?:[ \t]*(.*))$")
_TAG_AT_START_RE = re.compile(r"\A\s*\[([^\]]+)\]")


def validate_task_schema_shape(
    schema: Any, *, filename: str = "dish-task-schema.json"
) -> dict[str, Any]:
    """Validate the current external Honest schema envelope.

    Step 1 validates compatibility metadata, source traceability, protocol file
    routing, migrations, and the temporary legacy-validation projection. The
    canonical task grammar itself is implemented in Step 2.
    """

    if not isinstance(schema, dict):
        raise ReleaseResolutionError(
            "schema_malformed", f"{filename} must contain a JSON object"
        )

    required = {
        "schema_kind",
        "protocol_version",
        "schema_version",
        "protocol_files",
        "migration_files",
        "rules",
        "task_document",
        "legacy_validation_adapter",
    }
    missing = sorted(required - set(schema))
    if missing:
        raise ReleaseResolutionError(
            "schema_malformed",
            f"{filename} is missing required keys",
            keys=missing,
        )
    unknown = sorted(set(schema) - required)
    if unknown:
        raise ReleaseResolutionError(
            "schema_malformed",
            f"{filename} contains unknown top-level keys",
            keys=unknown,
        )
    if schema["schema_kind"] != "dish-task":
        raise ReleaseResolutionError(
            "schema_malformed", f"{filename} has the wrong schema_kind"
        )
    for key in ("protocol_version", "schema_version"):
        value = schema[key]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ReleaseResolutionError(
                "schema_malformed", f"{filename} {key} must be a non-empty string"
            )

    protocol_files = schema["protocol_files"]
    if not isinstance(protocol_files, dict) or set(protocol_files) != set(
        PROTOCOL_FILENAMES
    ):
        raise ReleaseResolutionError(
            "schema_malformed",
            f"{filename} protocol_files must declare exactly the supported roles",
            roles=sorted(PROTOCOL_FILENAMES),
        )
    for role, expected in PROTOCOL_FILENAMES.items():
        if protocol_files.get(role) != expected:
            raise ReleaseResolutionError(
                "schema_protocol_disagreement",
                f"{filename} maps {role} to an unexpected protocol file",
                role=role,
                expected=expected,
                actual=protocol_files.get(role),
            )

    migration_files = schema["migration_files"]
    if (
        not isinstance(migration_files, list)
        or not migration_files
        or not all(isinstance(item, str) and item.strip() == item and item for item in migration_files)
        or len(migration_files) != len(set(migration_files))
    ):
        raise ReleaseResolutionError(
            "schema_malformed", f"{filename} migration_files is malformed"
        )

    rules = schema["rules"]
    if not isinstance(rules, list) or not rules:
        raise ReleaseResolutionError(
            "schema_malformed", f"{filename} rules must be a non-empty list"
        )
    rule_ids: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or set(rule) != {
            "id",
            "source",
            "kind",
            "description",
        }:
            raise ReleaseResolutionError(
                "schema_malformed",
                f"{filename} rule {index} is malformed",
            )
        for key in ("id", "source", "kind", "description"):
            value = rule[key]
            if not isinstance(value, str) or not value.strip():
                raise ReleaseResolutionError(
                    "schema_malformed",
                    f"{filename} rule {index} has an empty {key}",
                )
        if rule["id"] in rule_ids:
            raise ReleaseResolutionError(
                "schema_malformed",
                f"{filename} contains duplicate rule identifiers",
                rule_id=rule["id"],
            )
        rule_ids.add(rule["id"] )
        source_file = rule["source"].split("#", 1)[0]
        if source_file not in set(PROTOCOL_FILENAMES.values()):
            raise ReleaseResolutionError(
                "schema_protocol_disagreement",
                f"{filename} rule source is not a governed protocol",
                rule_id=rule["id"],
                source=rule["source"],
            )

    task_document = schema["task_document"]
    if not isinstance(task_document, dict):
        raise ReleaseResolutionError(
            "schema_malformed", f"{filename} task_document must be an object"
        )
    expected_planning = [
        "Dish candidate",
        "Purpose",
        "Role",
        "Priors",
        "Locks",
        "Exemptions",
        "Research emphasis",
        "Destination section",
    ]
    expected_state = [
        "Status",
        "Status detail",
        "Resume status",
        "Verification protocol release",
        "Researched by",
        "Verified by",
        "Self-verified",
    ]
    if task_document.get("planning_brief_fields") != expected_planning:
        raise ReleaseResolutionError(
            "schema_protocol_disagreement",
            f"{filename} does not declare the approved eight-field Planning brief",
        )
    if task_document.get("state_fields") != expected_state:
        raise ReleaseResolutionError(
            "schema_protocol_disagreement",
            f"{filename} does not declare the approved seven-field state block",
        )
    if task_document.get("schema_metadata_label") != "Schema version":
        raise ReleaseResolutionError(
            "schema_protocol_disagreement",
            f"{filename} has the wrong task schema metadata label",
        )

    adapters = schema["legacy_validation_adapter"]
    if not isinstance(adapters, dict) or set(adapters) != set(MANIFEST_FILENAMES):
        raise ReleaseResolutionError(
            "schema_malformed",
            f"{filename} legacy_validation_adapter is malformed",
        )
    checked_adapters = {
        kind: validate_manifest_shape(
            adapters[kind],
            expected_kind=kind,
            filename=f"{filename} legacy_validation_adapter.{kind}",
        )
        for kind in MANIFEST_FILENAMES
    }

    checked = copy.deepcopy(schema)
    checked["legacy_validation_adapter"] = checked_adapters
    return checked


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

    title_schema = manifest.get("title")
    if expected_kind == "complete_task":
        if not isinstance(title_schema, dict) or set(title_schema) != {
            "role_tags",
            "marker_pattern",
            "marker_prefix",
            "marker_suffix",
            "separator",
            "unreviewed_blocker",
        }:
            raise ReleaseResolutionError(
                "manifest_malformed", f"{filename} title grammar is malformed"
            )
        role_tags = title_schema["role_tags"]
        if (
            not isinstance(role_tags, list)
            or not role_tags
            or not all(
                isinstance(role, str) and role and role == role.strip()
                for role in role_tags
            )
            or len(role_tags) != len(set(role_tags))
        ):
            raise ReleaseResolutionError(
                "manifest_malformed", f"{filename} title role_tags is malformed"
            )
        for key in ("marker_prefix", "marker_suffix", "separator"):
            value = title_schema[key]
            if (
                not isinstance(value, str)
                or not value
                or "\r" in value
                or "\n" in value
            ):
                raise ReleaseResolutionError(
                    "manifest_malformed", f"{filename} title {key} is malformed"
                )
        if any(
            value != value.strip()
            for value in (
                title_schema["marker_prefix"],
                title_schema["marker_suffix"],
            )
        ):
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} title marker controls contain whitespace",
            )
        if title_schema["marker_prefix"] == title_schema["marker_suffix"]:
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} title marker controls must differ",
            )
        try:
            marker_regex = re.compile(title_schema["marker_pattern"])
        except (TypeError, re.error) as exc:
            raise ReleaseResolutionError(
                "manifest_malformed", f"{filename} title marker_pattern is invalid"
            ) from exc
        unreviewed = title_schema["unreviewed_blocker"]
        if (
            not isinstance(unreviewed, str)
            or not unreviewed
            or unreviewed != unreviewed.strip()
            or unreviewed in role_tags
        ):
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} title unreviewed_blocker is malformed",
            )
        marker_values = [*role_tags, unreviewed]
        if any(marker_regex.fullmatch(value) is None for value in marker_values):
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} title marker grammar rejects a declared marker",
            )
        controls = (title_schema["marker_prefix"], title_schema["marker_suffix"])
        if any(any(control in value for control in controls) for value in marker_values):
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"{filename} title marker values contain control delimiters",
            )
    elif title_schema is not None:
        raise ReleaseResolutionError(
            "manifest_malformed", f"{filename} planning manifest must not define title grammar"
        )
    return copy.deepcopy(manifest)


def _error(rule: str, **fields: Any) -> dict[str, Any]:
    return {"rule": rule, **fields}


def _title_schema(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    checked = validate_manifest_shape(
        manifest,
        expected_kind="complete_task",
        filename="frozen complete-task manifest",
    )
    return checked["title"]


def _title_text_errors(
    value: Any, *, field: str, schema: Mapping[str, Any]
) -> tuple[str | None, list[dict[str, Any]]]:
    clean = str(value or "").strip()
    errors: list[dict[str, Any]] = []
    if not clean:
        errors.append(_error(f"title_{field}_required", field=field))
        return None, errors
    if "\r" in clean or "\n" in clean:
        errors.append(_error("title_line_break_forbidden", field=field))
    controls = (schema["marker_prefix"], schema["marker_suffix"])
    if any(control in clean for control in controls):
        errors.append(_error("title_control_character_forbidden", field=field))
    if schema["separator"] in clean:
        errors.append(_error("title_boundary_ambiguous", field=field))
    return clean, errors


def validate_title_declaration(
    manifest: Mapping[str, Any],
    *,
    dish_name: Any,
    recognition: Any,
    roles: Sequence[Any] | None,
    no_role_tags: bool,
    blockers: Sequence[Any] | None,
    no_blockers: bool,
) -> TitleValidationResult:
    """Validate one complete title declaration and render it canonically."""

    schema = _title_schema(manifest)
    errors: list[dict[str, Any]] = []
    clean_name, name_errors = _title_text_errors(
        dish_name, field="dish_name", schema=schema
    )
    clean_recognition, recognition_errors = _title_text_errors(
        recognition, field="recognition", schema=schema
    )
    errors.extend(name_errors)
    errors.extend(recognition_errors)

    supplied_roles = list(roles or ())
    if supplied_roles and no_role_tags:
        errors.append(_error("title_role_declaration_conflict"))
    elif not supplied_roles and not no_role_tags:
        errors.append(_error("title_role_declaration_required"))

    allowed_roles = list(schema["role_tags"])
    role_set = set(allowed_roles)
    cleaned_roles: list[str] = []
    for raw_role in supplied_roles:
        role = str(raw_role or "").strip()
        if not role or role not in role_set:
            errors.append(_error("unknown_title_role", role=role))
            continue
        cleaned_roles.append(role)
    duplicate_roles = sorted(
        {role for role in cleaned_roles if cleaned_roles.count(role) > 1}
    )
    if duplicate_roles:
        errors.append(_error("duplicate_title_role", roles=duplicate_roles))
    canonical_roles = tuple(role for role in allowed_roles if role in cleaned_roles)

    supplied_blockers = list(blockers or ())
    if supplied_blockers and no_blockers:
        errors.append(_error("title_blocker_declaration_conflict"))
    elif not supplied_blockers and not no_blockers:
        errors.append(_error("title_blocker_declaration_required"))

    marker_regex = re.compile(schema["marker_pattern"])
    cleaned_blockers: list[str] = []
    for raw_blocker in supplied_blockers:
        blocker = str(raw_blocker or "").strip()
        controls = (schema["marker_prefix"], schema["marker_suffix"])
        if (
            not blocker
            or marker_regex.fullmatch(blocker) is None
            or any(control in blocker for control in controls)
        ):
            errors.append(_error("invalid_title_blocker", blocker=blocker))
            continue
        if blocker in role_set:
            errors.append(_error("reserved_title_blocker", blocker=blocker))
            continue
        cleaned_blockers.append(blocker)
    duplicate_blockers = sorted(
        {blocker for blocker in cleaned_blockers if cleaned_blockers.count(blocker) > 1}
    )
    if duplicate_blockers:
        errors.append(
            _error("duplicate_title_blocker", blockers=duplicate_blockers)
        )
    canonical_blockers = tuple(dict.fromkeys(cleaned_blockers))

    if errors or clean_name is None or clean_recognition is None:
        return TitleValidationResult(errors=tuple(errors))

    fields = TitleFields(
        role_tags=canonical_roles,
        blockers=canonical_blockers,
        dish_name=clean_name,
        recognition=clean_recognition,
    )
    return TitleValidationResult(
        errors=(), title=render_title(fields, manifest), fields=fields
    )


def render_title(fields: TitleFields, manifest: Mapping[str, Any]) -> str:
    schema = _title_schema(manifest)
    markers = [*fields.role_tags, *fields.blockers]
    rendered_markers = " ".join(
        f"{schema['marker_prefix']}{marker}{schema['marker_suffix']}"
        for marker in markers
    )
    body = f"{fields.dish_name}{schema['separator']}{fields.recognition}"
    return f"{rendered_markers} {body}" if rendered_markers else body


def parse_canonical_title(
    title: Any, manifest: Mapping[str, Any]
) -> TitleValidationResult:
    """Parse a title only when it exactly matches the manifest grammar."""

    schema = _title_schema(manifest)
    raw = str(title or "")
    errors: list[dict[str, Any]] = []
    if not raw or raw != raw.strip():
        errors.append(_error("title_noncanonical_whitespace"))
    remaining = raw.strip()
    prefix = schema["marker_prefix"]
    suffix = schema["marker_suffix"]
    markers: list[str] = []
    while remaining.startswith(prefix):
        close = remaining.find(suffix, len(prefix))
        if close < 0:
            errors.append(_error("title_marker_unclosed"))
            return TitleValidationResult(errors=tuple(errors))
        marker = remaining[len(prefix):close]
        if not marker or re.fullmatch(schema["marker_pattern"], marker) is None:
            errors.append(_error("invalid_title_marker", marker=marker))
        markers.append(marker)
        tail = remaining[close + len(suffix):]
        if tail and not tail.startswith(" "):
            errors.append(_error("title_marker_spacing"))
            remaining = tail.lstrip()
            break
        remaining = tail[1:] if tail.startswith(" ") else tail

    separator = schema["separator"]
    if remaining.count(separator) != 1:
        errors.append(
            _error("title_boundary_ambiguous", count=remaining.count(separator))
        )
        return TitleValidationResult(errors=tuple(errors))
    dish_name, recognition = remaining.split(separator, 1)

    role_order = {role: index for index, role in enumerate(schema["role_tags"])}
    roles: list[str] = []
    blockers: list[str] = []
    blocker_seen = False
    last_role_index = -1
    for marker in markers:
        if marker in role_order:
            if blocker_seen:
                errors.append(_error("title_role_after_blocker", role=marker))
            if role_order[marker] < last_role_index:
                errors.append(_error("title_role_order_noncanonical", role=marker))
            last_role_index = max(last_role_index, role_order[marker])
            roles.append(marker)
        else:
            blocker_seen = True
            blockers.append(marker)

    declared = validate_title_declaration(
        manifest,
        dish_name=dish_name,
        recognition=recognition,
        roles=roles,
        no_role_tags=not roles,
        blockers=blockers,
        no_blockers=not blockers,
    )
    errors.extend(declared.errors)
    if declared.title is not None and declared.title != raw:
        errors.append(
            _error("title_noncanonical", expected=declared.title, actual=raw)
        )
    if errors or declared.fields is None or declared.title is None:
        return TitleValidationResult(errors=tuple(errors))
    return declared


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
