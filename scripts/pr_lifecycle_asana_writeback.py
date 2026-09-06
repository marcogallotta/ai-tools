"""Scoped, residual-gate-aware Asana reconciliation after authoritative GitHub merge."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import re
from typing import Any, Mapping, Protocol

from assigned_task_dependency_reconciliation import (
    DependencyEvidence,
    EvidenceState,
    reconcile_assigned_task_dependencies,
)
from pr_lifecycle_owner import owning_task_identity_from_references
from pr_lifecycle_support import FULL_SHA_RE, LifecycleError, PRLifecycle

LANDING_MARKER = "dish-source-landing:v1"
_FINAL_SOURCE_RE = re.compile(
    r"<!--\s*dish-source-work:v1\s+final_outstanding_gate=(?:true|1|yes)\s*-->", re.I
)
_RESIDUAL_RE = re.compile(
    r"<!--\s*dish-residual-gate:v1\s+kind=(?P<kind>runtime|test|postgresql|deployment|human|external)\s+state=(?:open|blocked|pending)\s*-->",
    re.I,
)
_DEPENDENT_RE_TEMPLATE = (
    r"<!--\s*dish-source-dependency:v1\s+upstream={upstream}\s+only_gate=(?:true|1|yes)\s*-->"
)


class WritebackAsana(Protocol):
    def get_task(self, gid: str) -> dict[str, Any]: ...
    def get_stories(self, gid: str) -> list[dict[str, Any]]: ...
    def add_comment(self, gid: str, text: str) -> dict[str, Any]: ...
    def update_task_fields(self, gid: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...
    def remove_dependency(self, task_gid: str, dependency_gid: str) -> None: ...


@dataclass(frozen=True)
class WritebackResult:
    task_gid: str
    landing_recorded: bool
    completed: bool
    residual_gates: tuple[str, ...]
    dependents_advanced: tuple[str, ...]

    def json(self) -> dict[str, Any]:
        value = asdict(self)
        value["residual_gates"] = list(self.residual_gates)
        value["dependents_advanced"] = list(self.dependents_advanced)
        return value


def landing_marker(*, repository: str, pr_number: int, head: str, merge_sha: str) -> str:
    identity = hashlib.sha256(f"{repository}:{pr_number}:{head}:{merge_sha}".encode()).hexdigest()[:20]
    return (
        f"<!-- {LANDING_MARKER} repo={repository} pr={pr_number} head={head} "
        f"merge={merge_sha} key={identity} -->"
    )


def _story_has_marker(stories: list[dict[str, Any]], marker: str) -> bool:
    return any(marker in str(story.get("text") or "") for story in stories)


def _residual_gates(task: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted({match.group("kind").lower() for match in _RESIDUAL_RE.finditer(str(task.get("notes") or ""))}))


def _combined_residual_gates(task: Mapping[str, Any], lifecycle: PRLifecycle) -> tuple[str, ...]:
    residual = set(_residual_gates(task))
    residual.update(f"post-merge:{gate}" for gate in lifecycle.post_merge_gates)
    return tuple(sorted(residual))


def reconcile_after_merge(
    *,
    asana: WritebackAsana,
    lifecycle: PRLifecycle,
    repository: str,
    merge_sha: str,
) -> WritebackResult:
    """Reconcile only facts mechanically established by authoritative merge readback.

    Task prose is never replaced. Landing evidence is appended as a comment; completion
    is scoped to the ``completed`` field and only when the task explicitly declares source
    landing as its final outstanding gate and no residual-domain marker remains.
    """
    if lifecycle.state.value != "merged":
        raise LifecycleError("post-merge Asana writeback requires authoritative MERGED lifecycle readback")
    if FULL_SHA_RE.fullmatch(lifecycle.head) is None or FULL_SHA_RE.fullmatch(merge_sha) is None:
        raise LifecycleError("post-merge Asana writeback requires exact source/merge SHAs")
    owner, owner_error = owning_task_identity_from_references(lifecycle.task_ids)
    if owner_error or owner is None:
        raise LifecycleError(f"post-merge Asana writeback requires one explicit owning task: {owner_error}")

    task = asana.get_task(owner)
    notes = str(task.get("notes") or "")
    residual = _combined_residual_gates(task, lifecycle)
    marker = landing_marker(
        repository=repository,
        pr_number=lifecycle.number,
        head=lifecycle.head,
        merge_sha=merge_sha,
    )
    stories = asana.get_stories(owner)
    recorded = _story_has_marker(stories, marker)
    if not recorded:
        asana.add_comment(
            owner,
            f"{marker}\nSOURCE LANDING VERIFIED — PR #{lifecycle.number} exact head `{lifecycle.head}` "
            f"merged as `{merge_sha}`. Runtime/deployment/acceptance authority remains separate.\n\n"
            "— Dish Agent: Integration | automated post-merge writeback",
        )
        if not _story_has_marker(asana.get_stories(owner), marker):
            raise LifecycleError("post-merge Asana landing evidence comment readback failed")
        recorded = True

    # Re-read after the landing comment before deciding completion. Comments and human
    # notes are separate Asana writes, so a concurrent residual-gate/decision edit must
    # be observed instead of completing from a stale task snapshot.
    pre_completion_task = asana.get_task(owner)
    notes = str(pre_completion_task.get("notes") or "")
    residual = _combined_residual_gates(pre_completion_task, lifecycle)
    should_complete = bool(_FINAL_SOURCE_RE.search(notes)) and not residual
    if should_complete and not bool(pre_completion_task.get("completed")):
        asana.update_task_fields(owner, {"completed": True})
    authoritative_task = asana.get_task(owner)
    if should_complete and not bool(authoritative_task.get("completed")):
        raise LifecycleError("post-merge Asana completion write readback failed")
    completed = bool(authoritative_task.get("completed"))

    advanced: list[str] = []
    for raw in authoritative_task.get("dependents") or []:
        if not isinstance(raw, Mapping) or not raw.get("gid"):
            continue
        dependent_gid = str(raw["gid"])
        dependent = asana.get_task(dependent_gid)
        pattern = re.compile(
            _DEPENDENT_RE_TEMPLATE.format(upstream=re.escape(owner)), re.I
        )
        if not pattern.search(str(dependent.get("notes") or "")):
            continue
        # Reuse the same exact-task dependency mutation/readback primitive used by explicit-assignment
        # admission. Post-merge reconciliation proves only this exact source edge satisfied; all other
        # dependencies remain unresolved here and lifecycle readiness remains a separate classification.
        def resolve_dependency_evidence(current: Mapping[str, Any]) -> dict[str, DependencyEvidence]:
            current_marker = pattern.search(str(current.get("notes") or "")) is not None
            evidence: dict[str, DependencyEvidence] = {}
            for dependency in current.get("dependencies") or []:
                if not isinstance(dependency, Mapping) or not dependency.get("gid"):
                    continue
                dependency_gid = str(dependency["gid"])
                if dependency_gid == owner and current_marker:
                    evidence[dependency_gid] = DependencyEvidence(
                        dependency_gid=dependency_gid,
                        state=EvidenceState.SATISFIED,
                        authority="github",
                        evidence_ref=(
                            f"PR #{lifecycle.number} exact head {lifecycle.head} merged as {merge_sha}"
                        ),
                    )
                else:
                    evidence[dependency_gid] = DependencyEvidence(
                        dependency_gid=dependency_gid,
                        state=EvidenceState.UNRESOLVED,
                        authority="outside-post-merge-writeback",
                        evidence_ref="not established by this exact source-landing reconciliation",
                    )
            return evidence

        reconciliation = reconcile_assigned_task_dependencies(
            asana=asana,
            task_gid=dependent_gid,
            resolve_evidence=resolve_dependency_evidence,
        )
        if owner in reconciliation.residual_dependencies and pattern.search(
            str(asana.get_task(dependent_gid).get("notes") or "")
        ):
            raise LifecycleError(
                f"dependent task {dependent_gid} source dependency removal readback failed"
            )
        if owner in reconciliation.removed_dependencies:
            advanced.append(dependent_gid)

    return WritebackResult(
        task_gid=owner,
        landing_recorded=recorded,
        completed=completed,
        residual_gates=residual,
        dependents_advanced=tuple(sorted(advanced)),
    )
