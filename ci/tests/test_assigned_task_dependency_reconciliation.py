from __future__ import annotations

from copy import deepcopy

from assigned_task_dependency_reconciliation import (
    Continuation,
    DependencyEvidence,
    EvidenceState,
    reconcile_assigned_task_dependencies,
)

TASK = "1218210684150441"
A = "1218208357807275"
B = "1218208357805283"
C = "1218208357804752"
D = "1218208484816068"


class FakeAsana:
    def __init__(self, dependencies, *, before_write=None, remove_fail=None, sticky=None):
        self.task = {"gid": TASK, "dependencies": [{"gid": gid} for gid in dependencies]}
        self.before_write = list(before_write or [])
        self.remove_fail = set(remove_fail or [])
        self.sticky = set(sticky or [])
        self.removals = []
        self.reads = 0

    def get_task(self, gid):
        assert gid == TASK
        self.reads += 1
        if self.before_write and self.reads == 2:
            self.task["dependencies"] = [{"gid": x} for x in self.before_write]
        return deepcopy(self.task)

    def remove_dependency(self, task_gid, dependency_gid):
        assert task_gid == TASK
        self.removals.append(dependency_gid)
        if dependency_gid not in self.sticky:
            self.task["dependencies"] = [
                x for x in self.task["dependencies"] if x["gid"] != dependency_gid
            ]
        if dependency_gid in self.remove_fail:
            raise RuntimeError("ambiguous transport failure")


def resolver(states):
    def resolve(task):
        return {
            item["gid"]: DependencyEvidence(
                dependency_gid=item["gid"],
                state=states.get(item["gid"], EvidenceState.AMBIGUOUS),
                authority="github",
                evidence_ref=f"exact:{item['gid']}",
            )
            for item in task.get("dependencies", [])
        }

    return resolve


def run(asana, states):
    return reconcile_assigned_task_dependencies(
        asana=asana,
        task_gid=TASK,
        resolve_evidence=resolver(states),
    )


def test_manual_merge_stale_edge_is_removed_and_continues():
    asana = FakeAsana([A])
    result = run(asana, {A: EvidenceState.SATISFIED})
    assert result.removed_dependencies == (A,)
    assert result.residual_dependencies == ()
    assert result.continuation is Continuation.CONTINUE


def test_four_satisfied_edges_are_all_removed():
    asana = FakeAsana([A, B, C, D])
    result = run(asana, {x: EvidenceState.SATISFIED for x in (A, B, C, D)})
    assert result.removed_dependencies == tuple(sorted((A, B, C, D)))
    assert asana.removals == sorted((A, B, C, D))
    assert result.continuation is Continuation.CONTINUE


def test_unmerged_exact_pr_remains_blocked_without_mutation():
    asana = FakeAsana([A])
    result = run(asana, {A: EvidenceState.UNRESOLVED})
    assert asana.removals == []
    assert result.residual_dependencies == (A,)
    assert result.continuation is Continuation.BLOCKED


def test_only_satisfied_edges_are_removed_when_one_is_unresolved():
    asana = FakeAsana([A, B])
    result = run(asana, {A: EvidenceState.SATISFIED, B: EvidenceState.UNRESOLVED})
    assert result.removed_dependencies == (A,)
    assert result.residual_dependencies == (B,)
    assert result.continuation is Continuation.BLOCKED


def test_ambiguous_edge_is_never_mutated():
    asana = FakeAsana([A])
    result = run(asana, {A: EvidenceState.AMBIGUOUS})
    assert asana.removals == []
    assert result.ambiguous_dependencies == (A,)
    assert result.continuation is Continuation.RECONCILIATION_REQUIRED


def test_missing_evidence_is_ambiguous():
    asana = FakeAsana([A])
    result = run(asana, {})
    assert asana.removals == []
    assert result.continuation is Continuation.RECONCILIATION_REQUIRED


def test_mismatched_evidence_identity_is_ambiguous():
    asana = FakeAsana([A])

    def bad(_task):
        return {A: DependencyEvidence(B, EvidenceState.SATISFIED, "github", "pr:1")}

    result = reconcile_assigned_task_dependencies(asana=asana, task_gid=TASK, resolve_evidence=bad)
    assert asana.removals == []
    assert result.ambiguous_dependencies == (A,)


def test_empty_authority_or_evidence_ref_is_ambiguous():
    asana = FakeAsana([A])

    def bad(_task):
        return {A: DependencyEvidence(A, EvidenceState.SATISFIED, "", "")}

    result = reconcile_assigned_task_dependencies(asana=asana, task_gid=TASK, resolve_evidence=bad)
    assert result.continuation is Continuation.RECONCILIATION_REQUIRED


def test_repeated_start_after_repair_is_idempotent():
    asana = FakeAsana([A])
    first = run(asana, {A: EvidenceState.SATISFIED})
    second = run(asana, {A: EvidenceState.SATISFIED})
    assert first.removed_dependencies == (A,)
    assert second.removed_dependencies == ()
    assert asana.removals == [A]


def test_write_error_with_successful_effect_is_reconciled_from_readback():
    asana = FakeAsana([A], remove_fail={A})
    result = run(asana, {A: EvidenceState.SATISFIED})
    assert result.removed_dependencies == (A,)
    assert result.continuation is Continuation.CONTINUE


def test_write_success_without_effect_is_reconciliation_required():
    asana = FakeAsana([A], sticky={A})
    result = run(asana, {A: EvidenceState.SATISFIED})
    assert result.removed_dependencies == ()
    assert result.ambiguous_dependencies == (A,)
    assert result.continuation is Continuation.RECONCILIATION_REQUIRED


def test_write_error_without_effect_is_reconciliation_required():
    asana = FakeAsana([A], remove_fail={A}, sticky={A})
    result = run(asana, {A: EvidenceState.SATISFIED})
    assert result.ambiguous_dependencies == (A,)
    assert result.continuation is Continuation.RECONCILIATION_REQUIRED


def test_dependency_set_race_recomputes_and_never_applies_stale_plan():
    asana = FakeAsana([A], before_write=[B])
    result = run(asana, {A: EvidenceState.SATISFIED, B: EvidenceState.UNRESOLVED})
    assert A not in asana.removals
    assert result.residual_dependencies == (B,)
    assert result.continuation is Continuation.BLOCKED


def test_dependency_change_during_evidence_resolution_is_rechecked_before_mutation():
    class StaleAttemptGuard(FakeAsana):
        def __init__(self):
            super().__init__([A])
            self.stale_attempts = []

        def remove_dependency(self, task_gid, dependency_gid):
            current = {item["gid"] for item in self.task["dependencies"]}
            if dependency_gid not in current:
                self.stale_attempts.append(dependency_gid)
            super().remove_dependency(task_gid, dependency_gid)

    asana = StaleAttemptGuard()
    calls = 0

    def moving_resolver(task):
        nonlocal calls
        calls += 1
        evidence = {
            item["gid"]: DependencyEvidence(
                dependency_gid=item["gid"],
                state=EvidenceState.SATISFIED if item["gid"] == A else EvidenceState.UNRESOLVED,
                authority="github",
                evidence_ref=f"exact:{item['gid']}",
            )
            for item in task.get("dependencies", [])
        }
        # The blocked implementation performed a second evidence resolution after its pre-write
        # reread. Move the live dependency set only if that second resolution still happens before
        # mutation; the corrected implementation must not reopen that stale-plan window.
        if calls == 2 and not asana.removals:
            asana.task["dependencies"] = [{"gid": B}]
        return evidence

    result = reconcile_assigned_task_dependencies(
        asana=asana,
        task_gid=TASK,
        resolve_evidence=moving_resolver,
    )
    assert asana.stale_attempts == []
    assert asana.removals == [A]
    assert result.residual_dependencies == ()
    assert result.continuation is Continuation.CONTINUE


def test_dependency_set_race_can_remove_new_exact_satisfied_edge():
    asana = FakeAsana([A], before_write=[B])
    result = run(asana, {A: EvidenceState.UNRESOLVED, B: EvidenceState.SATISFIED})
    assert asana.removals == [B]
    assert result.continuation is Continuation.CONTINUE


def test_unstable_dependency_set_fails_closed_after_bounded_reads():
    class Moving(FakeAsana):
        def get_task(self, gid):
            task = super().get_task(gid)
            self.task["dependencies"] = [{"gid": A if self.reads % 2 else B}]
            return task

    asana = Moving([A])
    result = run(asana, {A: EvidenceState.SATISFIED, B: EvidenceState.SATISFIED})
    assert result.continuation is Continuation.RECONCILIATION_REQUIRED


def test_new_unclassified_dependency_seen_at_final_readback_blocks_continuation():
    class AddAfterRemove(FakeAsana):
        def remove_dependency(self, task_gid, dependency_gid):
            super().remove_dependency(task_gid, dependency_gid)
            self.task["dependencies"].append({"gid": B})

    asana = AddAfterRemove([A])
    result = run(asana, {A: EvidenceState.SATISFIED})
    assert result.residual_dependencies == (B,)
    assert result.ambiguous_dependencies == (B,)
    assert result.continuation is Continuation.RECONCILIATION_REQUIRED


def test_result_json_uses_plain_values():
    asana = FakeAsana([A])
    payload = run(asana, {A: EvidenceState.SATISFIED}).json()
    assert payload["removed_dependencies"] == [A]
    assert payload["continuation"] == "CONTINUE"


def test_invalid_stability_read_count_is_rejected():
    asana = FakeAsana([])
    try:
        reconcile_assigned_task_dependencies(
            asana=asana,
            task_gid=TASK,
            resolve_evidence=resolver({}),
            max_stability_reads=0,
        )
    except ValueError as exc:
        assert "max_stability_reads" in str(exc)
    else:
        raise AssertionError("expected ValueError")
