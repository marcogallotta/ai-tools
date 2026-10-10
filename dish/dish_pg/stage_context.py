"""Stage-run governing context returned with a successful PostgreSQL ``start``.

The text is sourced from the exact Honest commit recorded on the
``HonestContractBinding`` that Dish froze for the run (the start execution's
binding, or the Verification cycle's binding), never from the current branch
tip. Each protocol file is verified against the per-file sha256 recorded on the
binding before it is served. Context assembly is response enrichment only: it
never changes authority, and any failure degrades to ``available: false`` so a
committed start is never reported as failed.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf

STAGE_CONTEXT_FORMAT = "dish-stage-context-v1"
# Combined UTF-8 text budget for every file served in one stage context. Current
# stage protocols are ~13-25 KB and CLAUDE.md ~26 KB; the budget keeps the reply
# well under the 1 MiB MCP conformance read ceiling.
STAGE_CONTEXT_MAX_TEXT_BYTES = 160 * 1024
REPOSITORY_INSTRUCTIONS_PATH = "CLAUDE.md"
STAGE_CONTEXT_GUIDANCE = (
    "The stage protocol is included here; do not re-read it from GitHub."
)
_START_KIND_ROLES = {
    "planning": "planning",
    "initial": "research",
    "change": "research",
    "verification": "verification",
}
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_GIT_TIMEOUT_SECONDS = 10
LOG = logging.getLogger("dish.stage_context")


class StageContextUnavailable(RuntimeError):
    """The frozen governing text cannot be served exactly."""


def role_for_start_kind(kind: object) -> str | None:
    return _START_KIND_ROLES.get(str(kind or ""))


def _safe_relative(path: object) -> str:
    raw = str(path or "")
    parts = PurePosixPath(raw)
    if (
        not raw
        or parts.is_absolute()
        or any(part in {"", ".", ".."} for part in parts.parts)
        or any(ord(char) < 32 for char in raw)
    ):
        raise StageContextUnavailable(f"binding names an unsafe Honest path: {raw!r}")
    return raw


def _git_blob(root: Path, commit: str, path: str) -> tuple[bytes, str]:
    spec = f"{commit}:{path}"
    try:
        object_id = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", spec],
            check=True, capture_output=True, timeout=_GIT_TIMEOUT_SECONDS,
        ).stdout.decode().strip()
        content = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", object_id],
            check=True, capture_output=True, timeout=_GIT_TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise StageContextUnavailable(
            f"{path} is unreachable at frozen Honest commit {commit}"
        ) from exc
    return content, object_id


def _file_entry(
    root: Path, commit: str, path: str, *, kind: str, expected_sha256: str | None
) -> dict[str, Any]:
    content, blob = _git_blob(root, commit, path)
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise StageContextUnavailable(
            f"{path} at {commit} does not match the sha256 frozen on the binding"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StageContextUnavailable(f"{path} is not UTF-8 text") from exc
    return {
        "kind": kind,
        "path": path,
        "git_commit": commit,
        "git_blob": blob,
        "sha256": digest,
        "sha256_verified_against_binding": expected_sha256 is not None,
        "bytes": len(content),
        "text": text,
    }


def frozen_binding_id(
    session: Session,
    *,
    role: str,
    request_id: uuid.UUID | None,
    data: Mapping[str, Any],
) -> uuid.UUID:
    """Return the binding Dish froze for this stage run."""

    if role == "verification" and data.get("cycle_id"):
        cycle = session.get(wf.VerificationCycle, uuid.UUID(str(data["cycle_id"])))
        if cycle is not None:
            return cycle.contract_binding_id
    if request_id is not None:
        execution = session.query(wf.CommandExecution).filter(
            wf.CommandExecution.request_id == request_id
        ).one_or_none()
        if execution is not None:
            return execution.contract_binding_id
    raise StageContextUnavailable("no frozen contract binding is recorded for this start")


def build_stage_context(
    binding: models.HonestContractBinding,
    *,
    role: str,
    honest_root: Path,
    max_text_bytes: int = STAGE_CONTEXT_MAX_TEXT_BYTES,
) -> dict[str, Any]:
    """Assemble the governing files at the binding's exact Honest commit."""

    source = binding.source_ids if isinstance(binding.source_ids, Mapping) else {}
    commit = str(source.get("commit") or "")
    if not _COMMIT_RE.fullmatch(commit):
        raise StageContextUnavailable("binding does not record an exact Honest commit")
    files = source.get("protocol_files")
    entry = files.get(role) if isinstance(files, Mapping) else None
    if not isinstance(entry, Mapping) or not entry.get("sha256"):
        raise StageContextUnavailable(f"binding does not freeze the {role} protocol file")
    root = Path(honest_root).expanduser().resolve()
    protocol = _file_entry(
        root, commit, _safe_relative(entry.get("path")),
        kind="stage_protocol", expected_sha256=str(entry["sha256"]),
    )
    if protocol["bytes"] > max_text_bytes:
        raise StageContextUnavailable(
            f"{protocol['path']} exceeds the {max_text_bytes}-byte stage context budget"
        )
    served = [protocol]
    omitted: list[dict[str, str]] = []
    try:
        instructions = _file_entry(
            root, commit, REPOSITORY_INSTRUCTIONS_PATH,
            kind="repository_instructions", expected_sha256=None,
        )
    except StageContextUnavailable as exc:
        omitted.append({"path": REPOSITORY_INSTRUCTIONS_PATH, "reason": str(exc)})
    else:
        if protocol["bytes"] + instructions["bytes"] > max_text_bytes:
            omitted.append({
                "path": REPOSITORY_INSTRUCTIONS_PATH,
                "reason": f"exceeds the {max_text_bytes}-byte stage context budget",
            })
        else:
            served.append(instructions)
    return {
        "available": True,
        "format": STAGE_CONTEXT_FORMAT,
        "role": role,
        "protocol_release": {
            "binding_id": str(binding.binding_id),
            "repository": str(source.get("repo") or "honest-pantry"),
            "honest_commit": commit,
            "honest_release": binding.honest_release,
            "protocol_version": binding.protocol_release,
            "protocol_bundle_sha256": binding.protocol_sha256,
            "schema_version": binding.schema_release,
            "schema_sha256": binding.schema_sha256,
        },
        "files": served,
        "omitted": omitted,
    }


def stage_context_for_start(
    session: Session,
    *,
    kind: object,
    request_id: uuid.UUID | None,
    data: Mapping[str, Any],
    honest_root: Path,
) -> dict[str, Any] | None:
    """Return the stage context for a successful start, or ``None`` if not a stage."""

    role = role_for_start_kind(kind)
    if role is None:
        return None
    try:
        binding_id = frozen_binding_id(session, role=role, request_id=request_id, data=data)
        binding = session.get(models.HonestContractBinding, binding_id)
        if binding is None:
            raise StageContextUnavailable("frozen contract binding row is missing")
        return build_stage_context(binding, role=role, honest_root=honest_root)
    except StageContextUnavailable as exc:
        reason = str(exc)
    except Exception as exc:  # enrichment must never fail a committed start
        LOG.exception("stage_context_failure role=%s", role)
        reason = f"stage context assembly failed ({type(exc).__name__})"
    return {
        "available": False,
        "format": STAGE_CONTEXT_FORMAT,
        "role": role,
        "reason": reason,
    }
