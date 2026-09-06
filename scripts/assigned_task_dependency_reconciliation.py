"""Bounded exact-task reconciliation for mechanically stale Asana dependencies."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol


class DependencyAsana(Protocol):
    def get_task(self, gid: str) -> dict[str, Any]: ...
    def remove_dependency(self, task_gid: str, dependency_gid: str) -> None: ...


class EvidenceState(str, Enum):
    SATISFIED = "SATISFIED"
    UNRESOLVED = "UNRESOLVED"
    AMBIGUOUS = "AMBIGUOUS"


class Continuation(str, Enum):
    CONTINUE = "CONTINUE"
    BLOCKED = "BLOCKED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class DependencyEvidence:
    """Authority-bound evidence for one exact dependency edge."""

    dependency_gid: str
    state: EvidenceState
    authority: str
    evidence_ref: str


@dataclass(frozen=True)
class ReconciliationResult:
    task_gid: str
    observed_dependencies: tuple[str, ...]
    removed_dependencies: tuple[str, ...]
    residual_dependencies: tuple[str, ...]
    ambiguous_dependencies: tuple[str, ...]
    continuation: Continuation

    def json(self) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "observed_dependencies",
            "removed_dependencies",
            "residual_dependencies",
            "ambiguous_dependencies",
        ):
            value[key] = list(value[key])
        value["continuation"] = self.continuation.value
        return value


EvidenceResolver = Callable[[Mapping[str, Any]], Mapping[str, DependencyEvidence]]


def dependency_gids(task: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(item["gid"])
            for item in (task.get("dependencies") or [])
            if isinstance(item, Mapping) and item.get("gid")
        )
    )


def _evidence_for(
    dependency_gid: str,
    evidence: Mapping[str, DependencyEvidence],
) -> DependencyEvidence:
    item = evidence.get(dependency_gid)
    if item is None:
        return DependencyEvidence(
            dependency_gid=dependency_gid,
            state=EvidenceState.AMBIGUOUS,
            authority="missing",
            evidence_ref="missing exact dependency authority",
        )
    if item.dependency_gid != dependency_gid or not item.authority or not item.evidence_ref:
        return DependencyEvidence(
            dependency_gid=dependency_gid,
            state=EvidenceState.AMBIGUOUS,
            authority="invalid",
            evidence_ref="evidence is not bound to the exact dependency identity/source",
        )
    return item


def reconcile_assigned_task_dependencies(
    *,
    asana: DependencyAsana,
    task_gid: str,
    resolve_evidence: EvidenceResolver,
    max_stability_reads: int = 3,
) -> ReconciliationResult:
    """Repair only mechanically satisfied dependency edges on one exact assigned task.

    The caller owns blocker interpretation and supplies an authority-aware resolver. This helper
    owns only bounded dependency mutation safety: fresh read, evidence binding, immediate pre-write
    reread/recompute, exact-edge removals, and authoritative final readback.
    """
    if not task_gid:
        raise ValueError("task_gid is required")
    if max_stability_reads < 1:
        raise ValueError("max_stability_reads must be >= 1")

    removed: set[str] = set()
    observed: set[str] = set()
    write_ambiguous: set[str] = set()

    stable_task: Mapping[str, Any] | None = None
    stable_evidence: Mapping[str, DependencyEvidence] = {}

    for _ in range(max_stability_reads):
        task = asana.get_task(task_gid)
        if str(task.get("gid") or task_gid) != task_gid:
            return ReconciliationResult(
                task_gid=task_gid,
                observed_dependencies=tuple(sorted(observed)),
                removed_dependencies=tuple(sorted(removed)),
                residual_dependencies=dependency_gids(task),
                ambiguous_dependencies=dependency_gids(task),
                continuation=Continuation.RECONCILIATION_REQUIRED,
            )
        deps = dependency_gids(task)
        observed.update(deps)
        evidence = resolve_evidence(task)

        # Re-read immediately before mutation. If the dependency set moved, throw away this plan
        # and resolve evidence again from the new authoritative task snapshot.
        prewrite = asana.get_task(task_gid)
        prewrite_deps = dependency_gids(prewrite)
        observed.update(prewrite_deps)
        if prewrite_deps != deps:
            continue
        stable_task = prewrite
        stable_evidence = evidence
        break

    if stable_task is None:
        final = asana.get_task(task_gid)
        final_deps = dependency_gids(final)
        observed.update(final_deps)
        return ReconciliationResult(
            task_gid=task_gid,
            observed_dependencies=tuple(sorted(observed)),
            removed_dependencies=tuple(sorted(removed)),
            residual_dependencies=final_deps,
            ambiguous_dependencies=final_deps,
            continuation=Continuation.RECONCILIATION_REQUIRED,
        )

    stable_deps = dependency_gids(stable_task)
    plan = {gid: _evidence_for(gid, stable_evidence) for gid in stable_deps}
    intended = [gid for gid, item in plan.items() if item.state is EvidenceState.SATISFIED]

    for dependency_gid in intended:
        try:
            asana.remove_dependency(task_gid, dependency_gid)
        except Exception:
            # The write may have succeeded despite a transport error. Final authoritative readback
            # decides whether the exact effect occurred; do not blindly retry here.
            write_ambiguous.add(dependency_gid)

    final = asana.get_task(task_gid)
    final_deps = dependency_gids(final)
    observed.update(final_deps)
    final_evidence = resolve_evidence(final)

    for dependency_gid in intended:
        if dependency_gid not in final_deps:
            removed.add(dependency_gid)
            write_ambiguous.discard(dependency_gid)
        else:
            write_ambiguous.add(dependency_gid)

    ambiguous = set(write_ambiguous)
    unresolved: set[str] = set()
    for dependency_gid in final_deps:
        item = _evidence_for(dependency_gid, final_evidence)
        if item.state is EvidenceState.AMBIGUOUS:
            ambiguous.add(dependency_gid)
        elif item.state is EvidenceState.UNRESOLVED:
            unresolved.add(dependency_gid)
        elif item.state is EvidenceState.SATISFIED:
            # A SATISFIED edge still present after the bounded write/readback cannot be called
            # repaired, even when the transport returned success.
            ambiguous.add(dependency_gid)

    if ambiguous:
        continuation = Continuation.RECONCILIATION_REQUIRED
    elif final_deps:
        continuation = Continuation.BLOCKED
    else:
        continuation = Continuation.CONTINUE

    return ReconciliationResult(
        task_gid=task_gid,
        observed_dependencies=tuple(sorted(observed)),
        removed_dependencies=tuple(sorted(removed)),
        residual_dependencies=final_deps,
        ambiguous_dependencies=tuple(sorted(ambiguous)),
        continuation=continuation,
    )
