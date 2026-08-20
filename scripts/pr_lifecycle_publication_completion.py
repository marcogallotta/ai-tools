"""Exact-byte local publication handoff and same-PR authoring finalization helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Mapping, Protocol, Sequence

from pr_lifecycle_support import AUTHORING_EVIDENCE_PENDING_RE, FULL_SHA_RE, LifecycleError
from installed_host_cert import EVIDENCE as INSTALLED_HOST_CERT_EVIDENCE, requirement_for_files, status_from_comments
from pr_lifecycle_owner import task_ids_from_pr

EXACT_BYTE_HANDOFF = "EXACT-BYTE HANDOFF"
FRESH_AUTHORING_REQUIRED = "FRESH AUTHORING REQUIRED"
DIRECT_CONNECTOR = "DIRECT CONNECTOR"
EXACT_BYTE_ARTIFACT_PUBLICATION = "EXACT-BYTE ARTIFACT/BUNDLE PUBLICATION"
PUBLICATION_BLOCKER_HEADING = "## PUBLICATION BLOCKER — LOCAL BRANCH COMPLETION REQUIRED BEFORE REVIEW"
_PUBLICATION_BLOCKER_RE = re.compile(
    rf"(?ms)^\s*{re.escape(PUBLICATION_BLOCKER_HEADING)}\s*\n.*?(?=^##\s+|\Z)"
)


class PublicationCompletionGitHub(Protocol):
    repository: str

    def get_pr(self, number: int) -> dict[str, Any]: ...
    def get_pr_files(self, number: int) -> list[dict[str, Any]]: ...
    def get_comments(self, number: int) -> list[dict[str, Any]]: ...
    def update_pr_body(self, number: int, body: str) -> dict[str, Any]: ...
    def mark_ready_for_review(self, number: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExactByteClassification:
    status: str
    bundle_path: str | None
    expected_head: str
    expected_tree: str
    observed_tree: str | None
    prior_evidence_transferable: bool
    reason: str

    @property
    def allowed(self) -> bool:
        return self.status == EXACT_BYTE_HANDOFF


@dataclass(frozen=True)
class PublicationRoute:
    route: str
    reason: str
    exact_byte_receiver_verification_required: bool
    prior_exact_tree_evidence_transferable: bool
    attempted_actions: tuple[str, ...]
    stop_reason: str | None


def classify_publication_route(
    *,
    connector_attempt_state: str,
    exact_candidate_bytes_available: bool,
    attempted_actions: Sequence[str] = (),
    stop_reason: str | None = None,
) -> PublicationRoute:
    """Keep GitHub connector publication primary until a real attempt degrades or fails.

    Candidate size, file count, or a prediction that the connector may be inconvenient is
    deliberately insufficient to select local completion. The fallback exists only after the
    normal connector path has actually been attempted and is failing, unavailable, or turning
    into manual blob/chunk/base64 work.
    """
    state = str(connector_attempt_state or "").strip().lower().replace("_", "-")
    attempts = tuple(str(item).strip() for item in attempted_actions if str(item).strip())
    stopped_because = str(stop_reason or "").strip() or None
    if state in {"not-attempted", "working"}:
        reason = (
            "normal GitHub connector publication must be attempted first; candidate size or file count alone "
            "does not justify local completion"
            if state == "not-attempted"
            else "the GitHub connector publication attempt is working; keep the normal remote path"
        )
        return PublicationRoute(
            route=DIRECT_CONNECTOR,
            reason=reason,
            exact_byte_receiver_verification_required=False,
            prior_exact_tree_evidence_transferable=True,
            attempted_actions=attempts,
            stop_reason=None,
        )
    if state not in {"failing", "slow-or-manual", "unavailable"}:
        raise LifecycleError(
            "connector attempt state must be not-attempted|working|failing|slow-or-manual|unavailable"
        )
    if not attempts or stopped_because is None:
        raise LifecycleError(
            "local publication fallback requires at least one concrete GitHub connector publication attempt "
            "and the exact reason that attempt stopped or degraded"
        )
    if exact_candidate_bytes_available:
        return PublicationRoute(
            route=EXACT_BYTE_ARTIFACT_PUBLICATION,
            reason=(
                "the attempted GitHub connector path is failing/unavailable or degrading into manual blob/chunk/base64 work; "
                "stop that loop and transfer the exact candidate as one receiver-readable bundle"
            ),
            exact_byte_receiver_verification_required=True,
            prior_exact_tree_evidence_transferable=False,
            attempted_actions=attempts,
            stop_reason=stopped_because,
        )
    return PublicationRoute(
        route=FRESH_AUTHORING_REQUIRED,
        reason=(
            "the attempted GitHub connector path cannot safely continue and the exact candidate bytes are unavailable; "
            "hashes/prose/sidecars cannot transport the candidate"
        ),
        exact_byte_receiver_verification_required=False,
        prior_exact_tree_evidence_transferable=False,
        attempted_actions=attempts,
        stop_reason=stopped_because,
    )


def render_publication_fallback_notice(route: PublicationRoute) -> str:
    """Render the mandatory concise operator explanation for any remote-publication stop."""
    if route.route == DIRECT_CONNECTOR:
        raise LifecycleError("working/direct connector publication has no fallback stop notice")
    if not route.attempted_actions or not route.stop_reason:
        raise LifecycleError("fallback notice requires attempted actions and a stop reason")
    return "\n".join(
        [
            f"Stopped GitHub connector publication because: {route.stop_reason}",
            "Tried: " + "; ".join(route.attempted_actions),
            f"Next route: {route.route}",
        ]
    )


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise LifecycleError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _require_sha(value: str, label: str) -> str:
    sha = str(value or "").strip().lower()
    if FULL_SHA_RE.fullmatch(sha) is None:
        raise LifecycleError(f"{label} must be an exact 40-character Git SHA")
    return sha


def _bundle_path(downloads_dir: Path, bundle_filename: str) -> Path:
    name = str(bundle_filename or "").strip()
    if not name or Path(name).name != name:
        raise LifecycleError("bundle filename must be one basename, not a path")
    root = downloads_dir.expanduser().resolve()
    return root / name


def classify_receiver_bundle(
    *,
    downloads_dir: Path,
    bundle_filename: str,
    expected_head: str,
    expected_tree: str,
) -> ExactByteClassification:
    """Verify the one required receiver download; ignore every unrelated/optional sidecar file."""
    expected_head = _require_sha(expected_head, "expected head")
    expected_tree = _require_sha(expected_tree, "expected tree")
    bundle = _bundle_path(downloads_dir, bundle_filename)
    if not bundle.is_file():
        return ExactByteClassification(
            status=FRESH_AUTHORING_REQUIRED,
            bundle_path=str(bundle),
            expected_head=expected_head,
            expected_tree=expected_tree,
            observed_tree=None,
            prior_evidence_transferable=False,
            reason="the named exact candidate bundle is not receiver-readable; sidecars cannot substitute for bytes",
        )

    try:
        advertised = _run(["git", "bundle", "list-heads", str(bundle)]).stdout.splitlines()
        advertised_heads = {line.split(maxsplit=1)[0].lower() for line in advertised if line.strip()}
        if expected_head not in advertised_heads:
            raise LifecycleError("bundle does not advertise the expected exact candidate head")
        with tempfile.TemporaryDirectory(prefix="dish-exact-byte-") as temp_dir:
            bare = Path(temp_dir) / "verify.git"
            _run(["git", "init", "--bare", str(bare)])
            _run(["git", "-C", str(bare), "bundle", "verify", str(bundle)])
            _run(["git", "-C", str(bare), "fetch", str(bundle), expected_head])
            observed_tree = _run(
                ["git", "-C", str(bare), "rev-parse", f"{expected_head}^{{tree}}"]
            ).stdout.strip().lower()
    except LifecycleError as exc:
        return ExactByteClassification(
            status=FRESH_AUTHORING_REQUIRED,
            bundle_path=str(bundle),
            expected_head=expected_head,
            expected_tree=expected_tree,
            observed_tree=None,
            prior_evidence_transferable=False,
            reason=f"exact candidate bundle cannot prove retrievable expected bytes: {exc}",
        )

    if observed_tree != expected_tree:
        return ExactByteClassification(
            status=FRESH_AUTHORING_REQUIRED,
            bundle_path=str(bundle),
            expected_head=expected_head,
            expected_tree=expected_tree,
            observed_tree=observed_tree,
            prior_evidence_transferable=False,
            reason=f"bundle tree mismatch: expected {expected_tree}, got {observed_tree}",
        )
    return ExactByteClassification(
        status=EXACT_BYTE_HANDOFF,
        bundle_path=str(bundle),
        expected_head=expected_head,
        expected_tree=expected_tree,
        observed_tree=observed_tree,
        prior_evidence_transferable=True,
        reason="the single receiver-readable bundle contains the exact expected candidate tree",
    )


def render_local_publication_handoff(
    *,
    task_gid: str,
    pr_url: str,
    branch: str,
    bundle_filename: str,
    expected_tree: str,
) -> str:
    """Return the intentionally short, copy/paste temporary-containment handoff."""
    expected_tree = _require_sha(expected_tree, "expected tree")
    bundle_name = Path(str(bundle_filename or "").strip()).name
    if not bundle_name or bundle_name != str(bundle_filename or "").strip():
        raise LifecycleError("bundle filename must be one basename")
    return "\n".join(
        [
            f"Task {task_gid} / {pr_url}. Start now; do not pause for confirmation.",
            f"Use only `~/Downloads/{bundle_name}`; that bundle is the only download you need. Ignore other files in `~/Downloads` and do not stop because `.sha256`, manifest, or other sidecars are absent.",
            f"Continue the same branch `{branch}`, verify the bundle tree is `{expected_tree}`, publish those exact bytes non-force, clear stale publication-blocker metadata, then finalize the same PR to ready only if no real pre-Review blocker remains; verify exact head + `draft=false`.",
            "Do not re-author semantic work unless the bundle itself is missing/unreadable. This grants no Review or Integration authority.",
        ]
    )


def strip_publication_blocker(body: str) -> str:
    cleaned = _PUBLICATION_BLOCKER_RE.sub("", str(body or ""), count=1)
    return cleaned.strip() + ("\n" if cleaned.strip() else "")


def _pr_head(pr: Mapping[str, Any]) -> str:
    head = pr.get("head") if isinstance(pr, Mapping) else None
    if not isinstance(head, Mapping):
        return ""
    return str(head.get("sha") or "").lower()


def _finalization_snapshot(pr: Mapping[str, Any]) -> dict[str, Any]:
    body = str(pr.get("body") or "")
    pending = AUTHORING_EVIDENCE_PENDING_RE.search(body)
    return {
        "number": int(pr.get("number") or 0),
        "head": _pr_head(pr),
        "draft": bool(pr.get("draft")),
        "state": str(pr.get("state") or ""),
        "authoring_evidence_pending": None if pending is None else pending.group("value").strip(),
        "publication_blocker_present": PUBLICATION_BLOCKER_HEADING in body,
    }


def _pre_review_blocker_reason(
    github: PublicationCompletionGitHub,
    pr: Mapping[str, Any],
    *,
    allow_publication_blocker_clear: bool = False,
) -> str | None:
    """Return the current authoritative pre-Review blocker, if any."""
    snapshot = _finalization_snapshot(pr)
    if snapshot["state"] != "open":
        return "PR is not open"
    if snapshot["authoring_evidence_pending"]:
        return f"authoring evidence remains pending: {snapshot['authoring_evidence_pending']}"
    if snapshot["publication_blocker_present"] and not allow_publication_blocker_clear:
        return "current publication blocker remains on the PR"

    try:
        requirement = requirement_for_files(github.get_pr_files(snapshot["number"]))
        if requirement is not None:
            head = pr.get("head") if isinstance(pr, Mapping) else None
            branch = str(head.get("ref") or "").strip() if isinstance(head, Mapping) else ""
            status = status_from_comments(
                github.get_comments(snapshot["number"]),
                repository=github.repository,
                pr_number=snapshot["number"],
                branch=branch,
                head=snapshot["head"],
                task_ids=task_ids_from_pr(pr),
                requirement=requirement,
            )
            if not status.passed:
                return (
                    f"{INSTALLED_HOST_CERT_EVIDENCE} remains pending: "
                    f"{status.error or 'certificate missing'}"
                )
    except (LifecycleError, AttributeError) as exc:
        return f"pre-Review blocker evaluation failed: {exc}"
    return None


def finalize_same_pr_for_review(
    github: PublicationCompletionGitHub,
    *,
    number: int,
    expected_head: str,
    clear_publication_blocker: bool = False,
    keep_draft_reason: str | None = None,
) -> dict[str, Any]:
    """Fail closed unless authoritative GitHub readback proves the same exact PR is review-ready."""
    expected_head = _require_sha(expected_head, "expected head")
    current = github.get_pr(number)
    before = _finalization_snapshot(current)
    if before["number"] != int(number):
        raise LifecycleError("GitHub PR readback returned the wrong PR identity")
    if before["head"] != expected_head:
        return {
            "complete": False,
            "reason": f"exact head moved before finalization: expected {expected_head}, got {before['head']}",
            "before": before,
            "after": before,
        }
    if keep_draft_reason:
        return {"complete": False, "reason": f"explicit keep-draft instruction: {keep_draft_reason}", "before": before, "after": before}

    blocker = _pre_review_blocker_reason(
        github,
        current,
        allow_publication_blocker_clear=clear_publication_blocker,
    )
    if blocker is not None:
        return {"complete": False, "reason": blocker, "before": before, "after": before}

    body = str(current.get("body") or "")
    if before["publication_blocker_present"]:
        try:
            github.update_pr_body(number, strip_publication_blocker(body))
            current = github.get_pr(number)
        except LifecycleError as exc:
            return {
                "complete": False,
                "reason": f"publication blocker metadata update/readback failed: {exc}",
                "before": before,
                "after": before,
            }
        cleared = _finalization_snapshot(current)
        if cleared["head"] != expected_head:
            return {
                "complete": False,
                "reason": "exact head moved while clearing stale publication blocker metadata",
                "before": before,
                "after": cleared,
            }
        if cleared["publication_blocker_present"]:
            return {
                "complete": False,
                "reason": "publication blocker metadata did not clear on authoritative readback",
                "before": before,
                "after": cleared,
            }
        blocker = _pre_review_blocker_reason(github, current)
        if blocker is not None:
            return {"complete": False, "reason": blocker, "before": before, "after": cleared}

    current = github.get_pr(number)
    pre_ready = _finalization_snapshot(current)
    if pre_ready["head"] != expected_head:
        return {
            "complete": False,
            "reason": "exact head moved before ready transition",
            "before": before,
            "after": pre_ready,
        }
    blocker = _pre_review_blocker_reason(github, current)
    if blocker is not None:
        return {"complete": False, "reason": blocker, "before": before, "after": pre_ready}
    if pre_ready["draft"]:
        try:
            github.mark_ready_for_review(number)
        except LifecycleError as exc:
            return {
                "complete": False,
                "reason": f"ready-for-review transition failed: {exc}",
                "before": before,
                "after": pre_ready,
            }

    try:
        final_pr = github.get_pr(number)
        final = _finalization_snapshot(final_pr)
    except LifecycleError as exc:
        return {
            "complete": False,
            "reason": f"ready-for-review authoritative readback failed: {exc}",
            "before": before,
            "after": pre_ready,
        }
    if final["head"] != expected_head:
        return {
            "complete": False,
            "reason": "exact head changed during ready-for-review transition/readback",
            "before": before,
            "after": final,
        }
    blocker = _pre_review_blocker_reason(github, final_pr)
    if blocker is not None:
        return {"complete": False, "reason": blocker, "before": before, "after": final}
    if final["draft"]:
        return {
            "complete": False,
            "reason": "ready-for-review transition/readback failed: live PR remains draft",
            "before": before,
            "after": final,
        }
    return {"complete": True, "reason": "same exact PR is authoritatively review-ready", "before": before, "after": final}
