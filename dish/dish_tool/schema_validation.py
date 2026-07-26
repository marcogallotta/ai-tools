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
    if not isinstance(manifest, Mapping):
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
