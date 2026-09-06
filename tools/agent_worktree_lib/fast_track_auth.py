from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from .common import fail, require_full_sha
from .state import _asana_json

AUTH_MARKER = "dish-fast-track-authorization:v1"
_AUTH_RE = re.compile(rf"<!--\s*{re.escape(AUTH_MARKER)}\s+(?P<payload>\{{.*\}})\s*-->")
_MODES = {"TRIVIAL", "FAST-TRACK"}
_VALIDATION = {"meaningful-readback", "executable-proof"}

# These paths change shared authority, runtime/database semantics, deployment/CI,
# or the fast-track guard itself. A per-change shortcut fails closed instead of
# trying to decide whether a particular edit within them is harmless.
_HIGH_CONSEQUENCE_PREFIXES = (
    ".github/workflows/",
    "ci/",
    "hooks/",
    "dish/dish_pg/",
    "dish/migrations/",
    "dish/docs/architecture/",
    "dish/docs/chatgpt-projects/",
    "dish/docs/agents/",
    "tools/agent_worktree_lib/",
)
_HIGH_CONSEQUENCE_EXACT = {
    "CLAUDE.md",
    "tools/agent-worktree",
    "scripts/pr_gate.py",
    "scripts/integration_certification.py",
}


@dataclass(frozen=True)
class FastTrackAuthorization:
    story_gid: str
    task_gid: str
    mode: str
    branch: str
    base_ref: str
    base_head: str
    paths: tuple[str, ...]
    marco_words: str
    skip_review: bool
    validation: str


def _fallback(reason: str) -> None:
    fail(
        "FAST_TRACK_FALLBACK_REQUIRED",
        f"{reason}; stop the shortcut and continue through the normal lifecycle",
    )


def _normalize_authorized_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "authorized paths must be non-empty literal repository paths")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw in {".", ".."} or any(part in {"", ".", ".."} for part in path.parts):
        fail("FAST_TRACK_AUTHORIZATION_INVALID", f"authorized path escapes or is not canonical: {raw!r}")
    normalized = path.as_posix()
    if normalized != raw or raw.startswith(":"):
        fail("FAST_TRACK_AUTHORIZATION_INVALID", f"authorized path is not canonical: {raw!r}")
    return normalized


def high_consequence_reason(path: str) -> str | None:
    if path in _HIGH_CONSEQUENCE_EXACT:
        return f"{path} is a protected shared-control path"
    for prefix in _HIGH_CONSEQUENCE_PREFIXES:
        if path.startswith(prefix):
            return f"{path} is under protected high-consequence prefix {prefix}"
    return None


def assert_fast_track_worktree(identity: Any, repo: Any) -> None:
    if identity.path == repo.primary_top or identity.git_dir == identity.common_dir:
        fail("PROTECTED_PRIMARY", "fast-track mutation refuses the shared primary checkout")
    if identity.branch == "main" or not str(identity.branch).startswith("agent/"):
        fail("BRANCH_MISMATCH", f"fast-track mutation requires an owned agent/* branch, found {identity.branch!r}")


def _parse_authorization_story(story: Mapping[str, Any], task_gid: str) -> FastTrackAuthorization:
    text = str(story.get("text") or "")
    matches = list(_AUTH_RE.finditer(text))
    if len(matches) != 1:
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "authorization story must contain exactly one fast-track marker")
    try:
        payload = json.loads(matches[0].group("payload"))
    except json.JSONDecodeError:
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "fast-track marker contains malformed JSON")
    if not isinstance(payload, dict):
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "fast-track marker payload must be an object")
    required = {"task", "mode", "branch", "base_ref", "base_head", "paths", "marco_words", "skip_review", "validation"}
    if set(payload) != required:
        missing = sorted(required - set(payload))
        extra = sorted(set(payload) - required)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unknown " + ", ".join(extra))
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "authorization marker fields are invalid: " + "; ".join(detail))

    marker_task = str(payload["task"])
    if marker_task != task_gid:
        fail("FAST_TRACK_AUTHORIZATION_INVALID", f"authorization is for task {marker_task}, not {task_gid}")
    mode = str(payload["mode"])
    if mode not in _MODES:
        fail("FAST_TRACK_AUTHORIZATION_INVALID", f"unsupported fast-track mode {mode!r}")
    branch = str(payload["branch"])
    base_ref = str(payload["base_ref"])
    base_head = require_full_sha(str(payload["base_head"]), "fast-track authorization base head")
    paths_raw = payload["paths"]
    if not isinstance(paths_raw, list) or not paths_raw:
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "authorization requires a non-empty exact path list")
    paths = tuple(_normalize_authorized_path(item) for item in paths_raw)
    if len(set(paths)) != len(paths):
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "authorization path list contains duplicates")
    marco_words = payload["marco_words"]
    if not isinstance(marco_words, str) or not marco_words.strip():
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "authorization must preserve Marco's exact non-empty words")
    skip_review = payload["skip_review"]
    if not isinstance(skip_review, bool):
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "skip_review must be boolean")
    if mode == "TRIVIAL" and not skip_review:
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "TRIVIAL authorization must explicitly record that formal Review is skipped")
    validation = str(payload["validation"])
    if validation not in _VALIDATION:
        fail("FAST_TRACK_AUTHORIZATION_INVALID", f"unsupported validation class {validation!r}")
    if mode == "TRIVIAL" and validation != "meaningful-readback":
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "TRIVIAL is non-product/non-runtime and uses only meaningful readback validation")

    return FastTrackAuthorization(
        story_gid=str(story.get("gid") or ""),
        task_gid=task_gid,
        mode=mode,
        branch=branch,
        base_ref=base_ref,
        base_head=base_head,
        paths=paths,
        marco_words=marco_words,
        skip_review=skip_review,
        validation=validation,
    )


def _live_authorization(task_gid: str, story_gid: str | None, state: Mapping[str, Any]) -> FastTrackAuthorization:
    if not story_gid:
        fail(
            "FAST_TRACK_AUTHORIZATION_REQUIRED",
            "fast-track tooling never self-authorizes; supply the GID of the pre-existing durable Marco authorization story",
        )
    stories = _asana_json(
        f"/tasks/{task_gid}/stories?opt_fields=gid,text,resource_subtype&limit=100",
        "live fast-track authorization stories",
    )
    if not isinstance(stories, list):
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "live task stories are not a list")
    matches = [story for story in stories if isinstance(story, dict) and str(story.get("gid") or "") == story_gid]
    if len(matches) != 1:
        fail(
            "FAST_TRACK_AUTHORIZATION_REQUIRED",
            f"authorization story {story_gid!r} is absent; agents cannot create shortcut authority locally",
        )
    auth = _parse_authorization_story(matches[0], task_gid)
    if auth.branch != str(state["branch"]):
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "authorization branch does not match the owned task lineage")
    if auth.base_ref != str(state["base_ref"]):
        fail("FAST_TRACK_AUTHORIZATION_INVALID", "authorization base ref does not match the owned task lineage")
    if auth.base_ref != "refs/heads/main":
        _fallback(f"authorization targets {auth.base_ref!r}, not protected primary target refs/heads/main")
    if auth.mode == "TRIVIAL":
        for path in auth.paths:
            reason = high_consequence_reason(path)
            if reason:
                _fallback(reason)
    return auth
