#!/usr/bin/env python3
"""Shared implementation primitives for the guarded dish workflow.

This module intentionally contains no command-line parsing.  The separate ``dish``
and ``dish-admin`` executables import it so schema, release, validation, backend,
result, state-transition, and audit behaviour cannot drift between command surfaces.
"""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


COOKING_PROJECT_GID = "1215089183018968"
DEFAULT_DB_PATH = Path("~/ai-tools/var/dish-tool.db").expanduser()

AGENT_FAMILIES = {
    "claude": "claude",
    "gpt": "gpt",
    "codex": "gpt",
}
FAMILIES = frozenset(AGENT_FAMILIES.values())
SUBMISSION_KINDS = frozenset({"planning", "initial", "change"})
CHANGE_LEVELS = frozenset({"small", "large"})
SUBMISSION_STATES = frozenset(
    {
        "drafting",
        "research_handoff",
        "awaiting_verification",
        "awaiting_human",
        "ready",
        "in_flight",
        "written",
        "consumed",
        "discarded",
        "uncertain",
    }
)
TERMINAL_STATES = frozenset({"consumed", "discarded"})
NONTERMINAL_STATES = SUBMISSION_STATES - TERMINAL_STATES

ALLOWED_ACTIONS_BY_STATE = {
    None: [],
    "drafting": ["prepare"],
    "research_handoff": ["prepare"],
    "awaiting_verification": ["approve", "reject"],
    "ready": ["submit"],
    "written": ["submit"],
    "awaiting_human": [],
    "in_flight": [],
    "uncertain": [],
    "consumed": [],
    "discarded": [],
}

EXIT_STATUS_BY_CODE = {
    "OK": 0,
    "INVALID_ARGUMENT": 2,
    "NOT_FOUND": 2,
    "UNMANAGED_TASK": 2,
    "VALIDATION_FAILED": 2,
    "WRONG_STATE": 3,
    "AGENT_MISMATCH": 3,
    "VERIFIER_FAMILY_MISMATCH": 3,
    "CONFLICT": 3,
    "HUMAN_ACTION_REQUIRED": 3,
    "BACKEND_REJECTED": 4,
    "BACKEND_UNCERTAIN": 5,
    "INTERNAL_ERROR": 1,
}
DEFAULT_RETRYABLE_BY_CODE = {
    "OK": False,
    "INVALID_ARGUMENT": False,
    "NOT_FOUND": False,
    "UNMANAGED_TASK": False,
    "VALIDATION_FAILED": True,
    "WRONG_STATE": False,
    "AGENT_MISMATCH": False,
    "VERIFIER_FAMILY_MISMATCH": False,
    "CONFLICT": False,
    "HUMAN_ACTION_REQUIRED": False,
    "BACKEND_REJECTED": True,
    "BACKEND_UNCERTAIN": False,
    "INTERNAL_ERROR": False,
}

# The SDK is configured with no automatic retries.  One request may spend at most
# 10 seconds connecting and 30 seconds waiting for a response.  Recovery waits 90
# seconds: 40 seconds maximum request lifetime plus a 30 second safety margin, with
# a further 20 seconds of conservatism for scheduler and clock granularity.
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30
ASANA_REQUEST_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
MAX_REQUEST_LIFETIME_SECONDS = CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS
RECOVERY_SAFETY_MARGIN_SECONDS = 30
RECOVERY_QUARANTINE_SECONDS = 90
assert RECOVERY_QUARANTINE_SECONDS > (
    MAX_REQUEST_LIFETIME_SECONDS + RECOVERY_SAFETY_MARGIN_SECONDS
)

RELEASE_VERSION_FILENAME = "protocol_release"
PROTOCOL_FILENAMES = {
    "planning": "dish-planning-protocol.md",
    "research": "dish-research-protocol.md",
    "verification": "dish-verification-protocol.md",
}
MANIFEST_FILENAMES = {
    "planning": "dish-planning-manifest.json",
    "complete_task": "dish-complete-task-manifest.json",
}
GOVERNED_RELEASE_FILENAMES = tuple(PROTOCOL_FILENAMES.values()) + tuple(
    MANIFEST_FILENAMES.values()
)
_RELEASE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 /_-]*):(?:[ \t]*(.*))$")
_TAG_AT_START_RE = re.compile(r"\A\s*\[([^\]]+)\]")

SCHEMA_VERSION = 1


class DishRuleError(Exception):
    """Expected, machine-classifiable workflow error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        rule: str | None = None,
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if code not in EXIT_STATUS_BY_CODE:
            raise ValueError(f"unknown result code: {code}")
        super().__init__(message)
        self.code = code
        self.rule = rule
        self.retryable = (
            DEFAULT_RETRYABLE_BY_CODE[code] if retryable is None else retryable
        )
        self.details = dict(details or {})


class ReleaseResolutionError(DishRuleError):
    def __init__(self, rule: str, message: str, **details: Any) -> None:
        super().__init__(
            "VALIDATION_FAILED",
            message,
            rule=rule,
            retryable=False,
            details=details,
        )


class BackendFailure(DishRuleError):
    """Mapped Asana/transport failure with application certainty attached."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        phase: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(code, message, retryable=retryable)
        self.status = status
        self.phase = phase


@dataclass(frozen=True)
class Section:
    gid: str
    name: str


@dataclass(frozen=True)
class SectionRegistry:
    by_name: Mapping[str, Section]
    by_gid: Mapping[str, Section]
    research_queue_gid: str
    verification_queue_gid: str
    sourcing_gid: str
    reference_gid: str

    @classmethod
    def from_sections(cls, sections: Iterable[Mapping[str, Any]]) -> "SectionRegistry":
        by_name: dict[str, Section] = {}
        by_gid: dict[str, Section] = {}
        for raw in sections:
            gid = str(raw.get("gid") or "").strip()
            name = str(raw.get("name") or "").strip()
            if not gid or not name:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "section is missing an immutable GID or display name",
                    rule="section_malformed",
                )
            if name in by_name or gid in by_gid:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    f"ambiguous Cooking section: {name!r} / {gid!r}",
                    rule="section_ambiguous",
                )
            section = Section(gid=gid, name=name)
            by_name[name] = section
            by_gid[gid] = section

        required = ("Research Queue", "Verification Queue", "Sourcing", "Reference")
        missing = [name for name in required if name not in by_name]
        if missing:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "required Cooking sections are missing",
                rule="section_missing",
                details={"sections": missing},
            )
        return cls(
            by_name=dict(by_name),
            by_gid=dict(by_gid),
            research_queue_gid=by_name["Research Queue"].gid,
            verification_queue_gid=by_name["Verification Queue"].gid,
            sourcing_gid=by_name["Sourcing"].gid,
            reference_gid=by_name["Reference"].gid,
        )

    @property
    def excluded_gids(self) -> frozenset[str]:
        return frozenset({self.sourcing_gid, self.reference_gid})

    @property
    def queue_gids(self) -> frozenset[str]:
        return frozenset({self.research_queue_gid, self.verification_queue_gid})


@dataclass(frozen=True)
class ResolvedRelease:
    version: str
    commit: str
    root: Path
    protocols: Mapping[str, str]
    manifests: Mapping[str, Mapping[str, Any]]
    manifest_texts: Mapping[str, str]

    def bundle_for_submission(self, submission_kind: str) -> dict[str, str]:
        if submission_kind == "planning":
            return {"planning": self.protocols["planning"]}
        if submission_kind in {"initial", "change"}:
            return {
                "research": self.protocols["research"],
                "verification": self.protocols["verification"],
            }
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"unknown submission kind: {submission_kind!r}",
            rule="invalid_submission_kind",
        )

    def manifest_for_submission(self, submission_kind: str) -> Mapping[str, Any]:
        if submission_kind == "planning":
            return copy.deepcopy(self.manifests["planning"])
        if submission_kind in {"initial", "change"}:
            return copy.deepcopy(self.manifests["complete_task"])
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"unknown submission kind: {submission_kind!r}",
            rule="invalid_submission_kind",
        )


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[dict[str, Any], ...]
    exemption_tags: tuple[str, ...] | None = None
    destination_name: str | None = None
    destination_gid: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ProcessIdentity:
    hostname: str
    pid: int
    process_start: str


@dataclass(frozen=True)
class WriteAttempt:
    attempt_id: str
    started_at: str
    identity: ProcessIdentity


class RequestPhase(str, Enum):
    PRE_SEND = "pre_send"
    POSSIBLY_SENT = "possibly_sent"
    RESPONSE_RECEIVED = "response_received"


@dataclass
class RequestPhaseTracker:
    phase: RequestPhase = RequestPhase.PRE_SEND

    def mark_send_started(self) -> None:
        self.phase = RequestPhase.POSSIBLY_SENT

    def mark_response_received(self) -> None:
        self.phase = RequestPhase.RESPONSE_RECEIVED


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def agent_family(agent: str) -> str:
    try:
        return AGENT_FAMILIES[agent]
    except KeyError as exc:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"invalid agent {agent!r}; expected claude, gpt, or codex",
            rule="invalid_agent",
        ) from exc


def opposite_family(family: str) -> str:
    if family == "claude":
        return "gpt"
    if family == "gpt":
        return "claude"
    raise DishRuleError(
        "INVALID_ARGUMENT",
        f"invalid agent family: {family!r}",
        rule="invalid_agent_family",
    )


def is_protocol_managed(
    current_section_gid: str | None, registry: SectionRegistry
) -> bool:
    """Fail closed: unresolved section membership is managed."""

    if not current_section_gid:
        return True
    return str(current_section_gid) not in registry.excluded_gids


def resolve_destination(
    name: str, gid: str, registry: SectionRegistry
) -> Section:
    clean_name = str(name).strip()
    clean_gid = str(gid).strip()
    section_by_name = registry.by_name.get(clean_name)
    section_by_gid = registry.by_gid.get(clean_gid)
    if section_by_name is None or section_by_gid is None:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "Destination section does not resolve inside Cooking",
            rule="destination_unresolved",
            details={"name": clean_name, "gid": clean_gid},
        )
    if section_by_name.gid != clean_gid or section_by_gid.name != clean_name:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "Destination section name/GID pair does not match",
            rule="destination_mismatch",
            details={"name": clean_name, "gid": clean_gid},
        )
    if clean_gid in registry.queue_gids:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "Destination section must not be a workflow queue",
            rule="destination_is_queue",
            details={"name": clean_name, "gid": clean_gid},
        )
    return section_by_gid


def allowed_actions_for_state(state: str | None) -> list[str]:
    if state not in ALLOWED_ACTIONS_BY_STATE:
        return []
    return list(ALLOWED_ACTIONS_BY_STATE[state])


def result_envelope(
    *,
    command: str,
    ok: bool = True,
    code: str = "OK",
    task_gid: str | None = None,
    submission_id: str | None = None,
    state: str | None = None,
    retryable: bool | None = None,
    allowed_actions: Sequence[str] | None = None,
    data: Mapping[str, Any] | None = None,
    errors: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if code not in EXIT_STATUS_BY_CODE:
        raise ValueError(f"unknown result code: {code}")
    if ok and code != "OK":
        raise ValueError("successful result must use code OK")
    if not ok and code == "OK":
        raise ValueError("failed result must use a failure code")
    if retryable is None:
        retryable = DEFAULT_RETRYABLE_BY_CODE[code]
    if allowed_actions is None:
        allowed_actions = allowed_actions_for_state(state)
    return {
        "ok": bool(ok),
        "command": command,
        "code": code,
        "task_gid": task_gid,
        "submission_id": submission_id,
        "state": state,
        "retryable": bool(retryable),
        "allowed_actions": list(allowed_actions),
        "data": dict(data or {}),
        "errors": [dict(error) for error in (errors or [])],
    }


def error_envelope(
    command: str,
    error: DishRuleError,
    *,
    task_gid: str | None = None,
    submission_id: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    rule_error = []
    if error.rule:
        item = {"rule": error.rule}
        item.update(error.details)
        rule_error.append(item)
    return result_envelope(
        command=command,
        ok=False,
        code=error.code,
        task_gid=task_gid,
        submission_id=submission_id,
        state=state,
        retryable=error.retryable,
        errors=rule_error,
        data={"message": str(error)},
    )


def exit_status(code: str) -> int:
    return EXIT_STATUS_BY_CODE.get(code, 1)


def _git(
    repo: Path, *args: str, check: bool = True, preserve_output: bool = False
) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise ReleaseResolutionError(
            "release_git_error",
            "unable to inspect protocol release Git worktree",
            stderr=stderr.strip(),
        ) from exc
    return completed.stdout if preserve_output else completed.stdout.strip()


def _find_release_file(git_root: Path) -> Path:
    matches = [
        path
        for path in git_root.rglob(RELEASE_VERSION_FILENAME)
        if ".git" not in path.parts and path.is_file()
    ]
    if not matches:
        raise ReleaseResolutionError(
            "release_missing", f"missing {RELEASE_VERSION_FILENAME}"
        )
    if len(matches) != 1:
        raise ReleaseResolutionError(
            "release_ambiguous",
            f"found {len(matches)} {RELEASE_VERSION_FILENAME} files",
            paths=[str(path.relative_to(git_root)) for path in matches],
        )
    return matches[0]


def _validate_manifest_shape(
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
    if not isinstance(tags, list) or not tags or not all(
        isinstance(tag, str) and tag for tag in tags
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


def resolve_release(worktree: str | os.PathLike[str]) -> ResolvedRelease:
    requested = Path(worktree).expanduser().resolve()
    if not requested.exists():
        raise ReleaseResolutionError(
            "release_missing", f"protocol worktree does not exist: {requested}"
        )
    try:
        git_root = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve()
    except ReleaseResolutionError as exc:
        raise ReleaseResolutionError(
            "release_git_error", "protocol release path is not a Git worktree"
        ) from exc

    release_file = _find_release_file(git_root)
    release_root = release_file.parent
    required_paths = [release_file] + [
        release_root / filename for filename in GOVERNED_RELEASE_FILENAMES
    ]
    missing = [path.name for path in required_paths if not path.is_file()]
    if missing:
        raise ReleaseResolutionError(
            "release_incomplete", "protocol release is incomplete", files=missing
        )

    relative_paths = [str(path.relative_to(git_root)) for path in required_paths]
    dirty = _git(git_root, "status", "--porcelain", "--", *relative_paths)
    if dirty:
        raise ReleaseResolutionError(
            "release_dirty",
            "protocol release has uncommitted or untracked changes",
            status=dirty.splitlines(),
        )
    for relative in relative_paths:
        try:
            _git(git_root, "ls-files", "--error-unmatch", "--", relative)
        except ReleaseResolutionError as exc:
            raise ReleaseResolutionError(
                "release_incomplete",
                "protocol release contains an untracked governed file",
                file=relative,
            ) from exc

    release_relative = str(release_file.relative_to(git_root))
    release_commits = _git(
        git_root, "log", "--format=%H", "--", release_relative
    ).splitlines()
    if not release_commits:
        raise ReleaseResolutionError(
            "release_incomplete", "protocol_release has no commit binding"
        )
    release_commit = release_commits[0]
    previous_release_commit = release_commits[1] if len(release_commits) > 1 else None

    governed_relative = [
        str((release_root / filename).relative_to(git_root))
        for filename in GOVERNED_RELEASE_FILENAMES
    ]
    later_governed_commits = _git(
        git_root,
        "log",
        "--format=%H",
        f"{release_commit}..HEAD",
        "--",
        *governed_relative,
    )
    if later_governed_commits:
        raise ReleaseResolutionError(
            "release_commit_mismatch",
            "governed files changed after the current protocol_release commit",
            commits=later_governed_commits.splitlines(),
        )
    if previous_release_commit is None:
        initial_commits = {
            _git(git_root, "log", "-1", "--format=%H", "--", relative)
            for relative in governed_relative
        }
        if initial_commits != {release_commit}:
            raise ReleaseResolutionError(
                "release_commit_mismatch",
                "the initial governed release was not committed atomically",
                commits=sorted(initial_commits),
            )
    else:
        release_window_commits = set(
            _git(
                git_root,
                "log",
                "--format=%H",
                f"{previous_release_commit}..{release_commit}",
                "--",
                *governed_relative,
            ).splitlines()
        )
        if not release_window_commits <= {release_commit}:
            raise ReleaseResolutionError(
                "release_commit_mismatch",
                "governed files changed before the wrapper advanced atomically",
                commits=sorted(release_window_commits),
            )
    for relative in relative_paths:
        try:
            _git(git_root, "cat-file", "-e", f"{release_commit}:{relative}")
        except ReleaseResolutionError as exc:
            raise ReleaseResolutionError(
                "release_incomplete",
                "a governed file did not exist at the protocol_release commit",
                file=relative,
            ) from exc

    committed_text = {
        path.name: _git(
            git_root,
            "show",
            f"{release_commit}:{path.relative_to(git_root).as_posix()}",
            preserve_output=True,
        )
        for path in required_paths
    }
    version = committed_text[RELEASE_VERSION_FILENAME].strip()
    if not version or not _RELEASE_VERSION_RE.fullmatch(version):
        raise ReleaseResolutionError(
            "release_malformed", "protocol_release contains an invalid version"
        )
    if previous_release_commit is not None:
        previous_version = _git(
            git_root,
            "show",
            f"{previous_release_commit}:{release_relative}",
            preserve_output=True,
        ).strip()
        if previous_version == version:
            raise ReleaseResolutionError(
                "release_version_not_advanced",
                "protocol_release version was reused instead of advanced",
                version=version,
            )

    protocols: dict[str, str] = {}
    for role, filename in PROTOCOL_FILENAMES.items():
        content = committed_text[filename]
        if not content.strip():
            raise ReleaseResolutionError(
                "release_incomplete", f"protocol file is empty: {filename}"
            )
        protocols[role] = content

    manifests: dict[str, dict[str, Any]] = {}
    manifest_texts: dict[str, str] = {}
    for kind, filename in MANIFEST_FILENAMES.items():
        raw = committed_text[filename]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"invalid JSON in {filename}",
                line=exc.lineno,
                column=exc.colno,
            ) from exc
        parsed = _validate_manifest_shape(
            parsed, expected_kind=kind, filename=filename
        )
        if parsed.get("protocol_release") != version:
            raise ReleaseResolutionError(
                "release_version_mismatch",
                f"{filename} is not bound to protocol_release {version}",
            )
        manifests[kind] = parsed
        manifest_texts[kind] = raw

    return ResolvedRelease(
        version=version,
        commit=release_commit,
        root=release_root,
        protocols=copy.deepcopy(protocols),
        manifests=copy.deepcopy(manifests),
        manifest_texts=copy.deepcopy(manifest_texts),
    )


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
        errors.append(
            _error("missing_exemption_explanation", field=grammar["label"])
        )
    return tuple(sorted(set(tags))), errors


def validate_note(note: str, manifest: Mapping[str, Any]) -> ValidationResult:
    """Validate only literal shape and the two narrow operational grammars."""

    # A release resolver already validates manifests, but commands may load a
    # frozen JSON value from SQLite, so validate its shape again before trusting it.
    checked = _validate_manifest_shape(
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
                errors.append(
                    _error(f"duplicate_{category}", field=value, count=count)
                )
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
            errors.append(
                _error("invalid_destination", field=destination_label)
            )
        else:
            destination_name = destination_match.group("name").strip()
            destination_gid = destination_match.group("gid").strip()
            if not destination_name or not destination_gid:
                errors.append(
                    _error("invalid_destination", field=destination_label)
                )

    return ValidationResult(
        errors=tuple(errors),
        exemption_tags=exemption_tags,
        destination_name=destination_name,
        destination_gid=destination_gid,
    )


_MIGRATION_1 = f"""
CREATE TABLE submissions (
    submission_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL,
    submission_kind TEXT NOT NULL CHECK (submission_kind IN ('planning','initial','change')),
    protocol_release TEXT NOT NULL,
    release_commit TEXT NOT NULL,
    protocol_bundle TEXT NOT NULL CHECK (json_valid(protocol_bundle)),
    canonical_manifest TEXT NOT NULL CHECK (json_valid(canonical_manifest)),
    baseline_exemption_tags TEXT CHECK (baseline_exemption_tags IS NULL OR json_valid(baseline_exemption_tags)),
    prepared_exemption_tags TEXT CHECK (prepared_exemption_tags IS NULL OR json_valid(prepared_exemption_tags)),
    destination_section_name TEXT,
    destination_section_gid TEXT,
    exemption_revision TEXT,
    editor_agent TEXT NOT NULL CHECK (editor_agent IN ('claude','gpt','codex')),
    editor_family TEXT NOT NULL CHECK (editor_family IN ('claude','gpt')),
    change_level TEXT CHECK (change_level IS NULL OR change_level IN ('small','large')),
    change_reason TEXT,
    failed_verification_passes INTEGER NOT NULL DEFAULT 0 CHECK (failed_verification_passes >= 0),
    baseline_verification_line TEXT,
    required_verifier_family TEXT CHECK (required_verifier_family IS NULL OR required_verifier_family IN ('claude','gpt')),
    verifier_agent TEXT CHECK (verifier_agent IS NULL OR verifier_agent IN ('claude','gpt','codex')),
    verifier_family TEXT CHECK (verifier_family IS NULL OR verifier_family IN ('claude','gpt')),
    status TEXT NOT NULL CHECK (status IN ({','.join(repr(state) for state in sorted(SUBMISSION_STATES))})),
    write_attempt_id TEXT UNIQUE,
    in_flight_at TEXT,
    in_flight_hostname TEXT,
    in_flight_pid INTEGER,
    in_flight_process_start TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    completed_at TEXT,
    research_queue_moved_at TEXT,
    notes_written_at TEXT,
    destination_moved_at TEXT
);
CREATE UNIQUE INDEX submissions_one_open_per_task
    ON submissions(task_gid)
    WHERE status NOT IN ('consumed', 'discarded');
CREATE INDEX submissions_status_idx ON submissions(status);

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    submission_id TEXT REFERENCES submissions(submission_id),
    task_gid TEXT,
    event_type TEXT NOT NULL,
    actor_agent TEXT CHECK (actor_agent IS NULL OR actor_agent IN ('claude','gpt','codex')),
    details TEXT NOT NULL CHECK (json_valid(details)),
    created_at TEXT NOT NULL
);
CREATE INDEX audit_events_submission_idx ON audit_events(submission_id, created_at);
CREATE INDEX audit_events_task_idx ON audit_events(task_gid, created_at);
CREATE INDEX audit_events_type_idx ON audit_events(event_type, created_at);
"""
MIGRATIONS = {1: _MIGRATION_1}


def initialize_database(
    path: str | os.PathLike[str] = DEFAULT_DB_PATH,
) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate_database(conn)
    return conn


def migrate_database(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        row[0] for row in conn.execute("SELECT version FROM schema_migrations")
    }
    for version in sorted(MIGRATIONS):
        if version in applied:
            continue
        script = MIGRATIONS[version]
        applied_at = utc_now().replace("'", "''")
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + script
                + f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ({version}, '{applied_at}');\n"
                + f"PRAGMA user_version = {version};\n"
                + "COMMIT;\n"
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def record_audit(
    conn: sqlite3.Connection,
    *,
    submission_id: str | None,
    task_gid: str | None,
    event_type: str,
    actor_agent: str | None,
    details: Mapping[str, Any],
    created_at: str | None = None,
) -> str:
    if actor_agent is not None:
        agent_family(actor_agent)
    event_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO audit_events (
            event_id, submission_id, task_gid, event_type,
            actor_agent, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            submission_id,
            task_gid,
            event_type,
            actor_agent,
            json.dumps(dict(details), sort_keys=True, separators=(",", ":")),
            created_at or utc_now(),
        ),
    )
    return event_id


_ALLOWED_SUBMISSION_UPDATE_COLUMNS = {
    "prepared_exemption_tags",
    "destination_section_name",
    "destination_section_gid",
    "exemption_revision",
    "editor_agent",
    "editor_family",
    "failed_verification_passes",
    "required_verifier_family",
    "verifier_agent",
    "verifier_family",
    "write_attempt_id",
    "in_flight_at",
    "in_flight_hostname",
    "in_flight_pid",
    "in_flight_process_start",
    "approved_at",
    "completed_at",
    "research_queue_moved_at",
    "notes_written_at",
    "destination_moved_at",
}


def transition_submission(
    conn: sqlite3.Connection,
    submission_id: str,
    expected_states: Iterable[str],
    target_state: str,
    *,
    updates: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    expected = tuple(sorted(set(expected_states)))
    if not expected or not set(expected) <= SUBMISSION_STATES:
        raise ValueError("expected_states contains an invalid state")
    if target_state not in SUBMISSION_STATES:
        raise ValueError("target_state is invalid")
    changes = dict(updates or {})
    unknown = set(changes) - _ALLOWED_SUBMISSION_UPDATE_COLUMNS
    if unknown:
        raise ValueError(f"unsupported submission update columns: {sorted(unknown)}")

    assignments = ["status = ?"] + [f"{column} = ?" for column in changes]
    params: list[Any] = [target_state, *changes.values(), submission_id, *expected]
    placeholders = ",".join("?" for _ in expected)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            f"""
            UPDATE submissions
               SET {', '.join(assignments)}
             WHERE submission_id = ?
               AND status IN ({placeholders})
            """,
            params,
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT status FROM submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            conn.execute("ROLLBACK")
            if row is None:
                raise DishRuleError(
                    "NOT_FOUND",
                    f"submission not found: {submission_id}",
                    rule="submission_not_found",
                )
            raise DishRuleError(
                "WRONG_STATE",
                f"submission is {row['status']}, expected one of {expected}",
                rule="wrong_state",
                details={"actual": row["status"], "expected": list(expected)},
            )
        row = conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        conn.execute("COMMIT")
        return row
    except DishRuleError:
        raise
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _linux_process_start(pid: int) -> str | None:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        stat = stat_path.read_text()
        close_paren = stat.rfind(")")
        if close_paren < 0:
            return None
        tail = stat[close_paren + 2 :].split()
        start_ticks = tail[19]
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            boot_id = "unknown-boot"
        return f"{boot_id}:{start_ticks}"
    except (OSError, IndexError):
        return None


def current_process_identity() -> ProcessIdentity:
    pid = os.getpid()
    process_start = _linux_process_start(pid)
    if process_start is None:
        process_start = f"fallback:{pid}"
    return ProcessIdentity(
        hostname=socket.gethostname(),
        pid=pid,
        process_start=process_start,
    )


def process_identity_is_live(identity: ProcessIdentity) -> bool:
    # A different host cannot be inspected safely from this local-only tool, so
    # recovery fails closed and treats that recorded process as live.
    if identity.hostname != socket.gethostname():
        return True
    current_start = _linux_process_start(identity.pid)
    if current_start is not None:
        return current_start == identity.process_start
    try:
        os.kill(identity.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return identity.process_start == f"fallback:{identity.pid}"


def begin_write_attempt(
    conn: sqlite3.Connection, submission_id: str
) -> WriteAttempt:
    attempt = WriteAttempt(
        attempt_id=str(uuid.uuid4()),
        started_at=utc_now(),
        identity=current_process_identity(),
    )
    transition_submission(
        conn,
        submission_id,
        {"ready"},
        "in_flight",
        updates={
            "write_attempt_id": attempt.attempt_id,
            "in_flight_at": attempt.started_at,
            "in_flight_hostname": attempt.identity.hostname,
            "in_flight_pid": attempt.identity.pid,
            "in_flight_process_start": attempt.identity.process_start,
        },
    )
    return attempt


def finish_write_attempt(
    conn: sqlite3.Connection,
    submission_id: str,
    *,
    attempt_id: str,
    target_state: str,
) -> sqlite3.Row:
    if target_state not in {"ready", "written", "uncertain"}:
        raise ValueError("write attempt target must be ready, written, or uncertain")
    updates: dict[str, Any] = {}
    if target_state == "ready":
        updates.update(
            {
                "write_attempt_id": None,
                "in_flight_at": None,
                "in_flight_hostname": None,
                "in_flight_pid": None,
                "in_flight_process_start": None,
            }
        )
    elif target_state == "written":
        updates["notes_written_at"] = utc_now()

    assignments = ["status = ?"] + [f"{column} = ?" for column in updates]
    params = [target_state, *updates.values(), submission_id, attempt_id]
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            f"""
            UPDATE submissions
               SET {', '.join(assignments)}
             WHERE submission_id = ?
               AND status = 'in_flight'
               AND write_attempt_id = ?
            """,
            params,
        )
        if cursor.rowcount != 1:
            conn.execute("ROLLBACK")
            raise DishRuleError(
                "CONFLICT",
                "write attempt no longer owns this submission",
                rule="stale_write_attempt",
            )
        row = conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        conn.execute("COMMIT")
        return row
    except DishRuleError:
        raise
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def load_asana_pat() -> str:
    pat = os.environ.get("ASANA_PAT")
    if pat:
        return pat
    env_path = Path(
        os.environ.get("ASANA_ENV", "~/.config/asana-cli/.env")
    ).expanduser()
    try:
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if line.startswith("ASANA_PAT="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    except FileNotFoundError:
        pass
    raise DishRuleError(
        "INTERNAL_ERROR",
        f"ASANA_PAT not found (set ASANA_PAT or add it to {env_path})",
        rule="asana_auth_missing",
    )


def asana_error_detail(error: Exception, context: str | None = None) -> str:
    status = getattr(error, "status", None)
    body = getattr(error, "body", None)
    reason = getattr(error, "reason", None)
    if isinstance(body, (bytes, bytearray)):
        body = body.decode(errors="replace")
    detail = str(body or reason or error)[:800]
    where = f" [{context}]" if context else ""
    if status == 401:
        return f"Asana auth error (401){where}: {detail}"
    if status == 404:
        return f"Asana resource not found (404){where}: {detail}"
    if status == 429:
        return f"Asana rate limit (429){where}: {detail}"
    if status is not None and status >= 500:
        return f"Asana server error ({status}){where}: {detail}"
    return f"Asana API error ({status}){where}: {detail}"


def map_backend_exception(
    error: Exception,
    *,
    phase: RequestPhase,
    context: str | None = None,
) -> BackendFailure:
    status = getattr(error, "status", None)
    if status is not None:
        message = asana_error_detail(error, context)
        if status == 408 or status >= 500:
            return BackendFailure(
                "BACKEND_UNCERTAIN",
                message,
                status=status,
                phase=phase.value,
                retryable=False,
            )
        return BackendFailure(
            "BACKEND_REJECTED",
            message,
            status=status,
            phase=phase.value,
            retryable=True,
        )
    if phase == RequestPhase.PRE_SEND:
        return BackendFailure(
            "BACKEND_REJECTED",
            f"backend request failed before transmission: {error}",
            phase=phase.value,
            retryable=True,
        )
    return BackendFailure(
        "BACKEND_UNCERTAIN",
        f"backend request may have been transmitted: {error}",
        phase=phase.value,
        retryable=False,
    )


class AsanaBackend:
    """Small SDK construction/call layer shared by both command surfaces."""

    def __init__(self, api_client: Any | None = None) -> None:
        self._client = api_client

    def client(self) -> Any:
        if self._client is None:
            try:
                import asana
                from urllib3.util import Retry
            except ImportError as exc:
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "python-asana is not installed",
                    rule="asana_sdk_missing",
                ) from exc
            config = asana.Configuration()
            config.access_token = load_asana_pat()
            config.return_page_iterator = False
            config.retry_strategy = Retry(total=0, connect=0, read=0, redirect=0)
            self._client = asana.ApiClient(config)
        return self._client

    def call(
        self,
        function: Any,
        *args: Any,
        context: str | None = None,
        phase_tracker: RequestPhaseTracker | None = None,
        **kwargs: Any,
    ) -> Any:
        tracker = phase_tracker or RequestPhaseTracker()
        try:
            tracker.mark_send_started()
            response = function(
                *args, _request_timeout=ASANA_REQUEST_TIMEOUT, **kwargs
            )
            tracker.mark_response_received()
            if not isinstance(response, Mapping) or "data" not in response:
                raise ValueError("Asana response missing data envelope")
            return response["data"]
        except BackendFailure:
            raise
        except Exception as exc:
            raise map_backend_exception(
                exc, phase=tracker.phase, context=context
            ) from exc
