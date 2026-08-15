"""Lifecycle classification from durable GitHub/Asana state."""
from __future__ import annotations

from pr_lifecycle_support import *
from pr_lifecycle_helpers import *
from pr_lifecycle_helpers import (
    _integration_order_reason, _lease_json, _mergeability_reason,
    _pr_base, _pr_branch, _pr_number, _pr_title, _pr_url,
    _reviewed_head, _utcnow,
)

class LifecycleInspectMixin:
    def __init__(
        self,
        github: GitHubBackend,
        *,
        asana: AsanaBackend | None = None,
        integration_authority: bool = False,
        integration_capable: bool = True,
        merge_method: str = "squash",
        now: Callable[[], datetime] = _utcnow,
        mutation_broker_enabled: bool = False,
        mutation_broker_repository_id: int | None = None,
        mutation_broker_routes: Mapping[str, str] | None = None,
    ) -> None:
        self.github = github
        self.asana = asana
        self.integration_authority = integration_authority
        self.integration_capable = integration_capable
        self.merge_method = merge_method
        self.now = now
        self.mutation_broker_enabled = mutation_broker_enabled
        self.mutation_broker_repository_id = mutation_broker_repository_id
        self.mutation_broker_routes = dict(mutation_broker_routes or {})
        self.integration_reconciler = None

    def _asana_details(self, task_ids: list[str]) -> list[dict[str, Any]]:
        if not self.asana:
            return []
        values: list[dict[str, Any]] = []
        for gid in task_ids:
            try:
                values.append(self.asana.get_task(gid))
            except LifecycleError as exc:
                values.append({"gid": gid, "error": str(exc)})
        return values


    def _external_dependency_for_failed_check(
        self,
        comments: list[dict[str, Any]],
        *,
        check_identity: str,
    ) -> tuple[ExternalDependency | None, str | None]:
        try:
            record = parse_external_dependency(comments)
        except LifecycleError as exc:
            return None, f"external dependency marker invalid: {exc}"
        if record is None or record.action == "resolved" or record.check != check_identity:
            return None, None

        status_reason = record.reason
        if record.owner_pr is not None:
            try:
                owner = self.github.get_pr(record.owner_pr)
            except (LifecycleError, AssertionError):
                owner = None
            if owner is not None:
                if bool(owner.get("merged") or owner.get("merged_at")):
                    return None, f"external owner PR #{record.owner_pr} is merged; re-evaluate exact-head evidence"
                owner_state = pr_gate.pr_state(owner)
                if owner_state != "open":
                    status_reason = (
                        f"external owner PR #{record.owner_pr} is closed unmerged; dependency remains unresolved"
                    )
        elif self.asana is not None:
            try:
                owner_task = self.asana.get_task(record.task_gid)
            except LifecycleError:
                owner_task = None
            if owner_task is not None and bool(owner_task.get("completed")):
                return None, f"external owner task {record.task_gid} is complete; re-evaluate exact-head evidence"

        return record, status_reason


    def inspect(self, pr: dict[str, Any]) -> PRLifecycle:
        number = _pr_number(pr)
        current = self.github.get_pr(number)
        head = pr_gate.pr_head_sha(current)
        state = pr_gate.pr_state(current)
        merged = bool(current.get("merged") or current.get("merged_at"))
        draft = pr_gate.pr_is_draft(current) if state == "open" else bool(current.get("draft", False))
        task_ids = task_ids_from_pr(current)
        base_kwargs = dict(
            number=number,
            url=_pr_url(current, self.github.repository),
            title=_pr_title(current),
            head=head,
            branch=_pr_branch(current),
            base=_pr_base(current),
            draft=draft,
            task_ids=task_ids,
            asana=self._asana_details(task_ids),
        )
        if merged:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.MERGED,
                state_label=STATE_LABELS[LifecycleState.MERGED],
            )
        if state != "open":
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.CLOSED,
                state_label=STATE_LABELS[LifecycleState.CLOSED],
                residual_reason="PR is closed without authoritative merged state",
            )

        comments = self.github.get_comments(number)
        reviews = self.github.get_reviews(number)
        now = self.now()
        leases = parse_leases(comments, current_head=head, reviews=reviews, pr_open=True, now=now)
        active_by_phase: dict[str, list[Lease]] = {}
        for lease in leases:
            active_by_phase.setdefault(lease.phase, []).append(lease)
        exact_review = pr_gate.latest_exact_head_review(reviews, reviewed_head=head)
        review_class = review_class_for(current, reviews, comments, current_head=head)
        lease_payload = [_lease_json(lease, now) for lease in leases]

        if draft:
            pending_evidence = pending_authoring_evidence(current)
            if pending_evidence:
                return implementation_continuation_lifecycle(
                    base_kwargs=base_kwargs,
                    evidence=pending_evidence,
                    review_class=review_class,
                    lease_payload=lease_payload,
                    implementation_active=bool(active_by_phase.get("implementation")),
                )
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.AUTHORING,
                state_label=STATE_LABELS[LifecycleState.AUTHORING],
                review_class=review_class,
                active_leases=lease_payload,
                residual_reason=(
                    "draft PR; ordinary Review discovery is excluded"
                    + ("; implementation lease active" if active_by_phase.get("implementation") else "")
                ),
            )

        if exact_review is None:
            if active_by_phase.get("review"):
                return PRLifecycle(
                    **base_kwargs,
                    state=LifecycleState.REVIEW_IN_PROGRESS,
                    state_label=STATE_LABELS[LifecycleState.REVIEW_IN_PROGRESS],
                    review_class=review_class,
                    active_leases=lease_payload,
                    residual_reason="active exact-head review lease; formal exact-head review not yet submitted",
                )
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_READY,
                state_label=STATE_LABELS[LifecycleState.REVIEW_READY],
                review_class=review_class,
                active_leases=lease_payload,
                residual_reason="no authoritative formal review exists for the exact current head",
            )

        verdict = str(exact_review.get("verdict"))
        reviewed_head = _reviewed_head(exact_review)
        if verdict == "BLOCK":
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.CHANGES_REQUESTED,
                state_label=STATE_LABELS[LifecycleState.CHANGES_REQUESTED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                residual_reason=(
                    "exact-head formal review blocks integration"
                    + ("; fix lease active" if active_by_phase.get("fix") or active_by_phase.get("implementation") else "; no active fix lease")
                ),
            )

        if verdict != "MERGE":
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_READY,
                state_label=STATE_LABELS[LifecycleState.REVIEW_READY],
                review_class=review_class,
                active_leases=lease_payload,
                residual_reason="formal review did not contain an authoritative exact-head verdict",
            )

        local_work = local_work_from_review(exact_review, comments, head=head)
        local_payload = [asdict(item) for item in local_work]
        pending_impl = next((item for item in local_work if item.kind == "implementation" and not item.completed), None)
        if pending_impl:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED,
                state_label=STATE_LABELS[LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                residual_reason=pending_impl.instruction,
                human_action=pending_impl.instruction if pending_impl.handoff_present else None,
            )
        pending_cert = next((item for item in local_work if item.kind == "certification" and not item.completed), None)
        if pending_cert:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.LOCAL_CERTIFICATION_REQUIRED,
                state_label=STATE_LABELS[LifecycleState.LOCAL_CERTIFICATION_REQUIRED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                residual_reason=pending_cert.instruction,
                human_action=pending_cert.instruction if pending_cert.handoff_present else None,
            )

        body = str(exact_review.get("body") or "")
        if TESTS_TO_RUN_RE.search(body) is None:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_PASSED,
                state_label=STATE_LABELS[LifecycleState.REVIEW_PASSED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                residual_reason="exact-head MERGE review is missing required TESTS TO RUN line",
            )

        ordering = _integration_order_reason(exact_review, current)
        if ordering:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_PASSED,
                state_label=STATE_LABELS[LifecycleState.REVIEW_PASSED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                residual_reason=f"integration ordering/dependency remains: {ordering}",
            )
        mergeability = _mergeability_reason(current)
        if mergeability:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_PASSED,
                state_label=STATE_LABELS[LifecycleState.REVIEW_PASSED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                residual_reason=mergeability,
            )

        try:
            combined = self.github.get_combined_status(head)
            runs = self.github.get_workflow_runs()
        except LifecycleError as exc:
            gate = {
                "diagnosis": pr_gate.GateDiagnosis.INFRASTRUCTURE_ERROR.value,
                "current_head": head,
                "reviewed_head": reviewed_head,
                "required_status_context": pr_gate.REQUIRED_ORDINARY_CI_CONTEXT,
                "reason": str(exc),
            }
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_PASSED,
                state_label=STATE_LABELS[LifecycleState.REVIEW_PASSED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                gate=gate,
                residual_reason=f"Integration evidence read failed: {exc}",
            )

        try:
            diagnosis = pr_gate.diagnose_integration_gate(
                current,
                reviewed_head=reviewed_head,
                reviewed_at=str(exact_review.get("submitted_at") or exact_review.get("submittedAt") or ""),
                combined_status=combined,
                workflow_runs=runs,
            )
        except pr_gate.GateError as exc:
            diagnosis = {
                "diagnosis": pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value,
                "current_head": head,
                "reviewed_head": reviewed_head,
                "required_status_context": pr_gate.REQUIRED_ORDINARY_CI_CONTEXT,
                "reason": str(exc),
            }

        diagnosis_state = diagnosis["diagnosis"]
        if diagnosis_state == pr_gate.GateDiagnosis.PENDING.value:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.WAITING_CI,
                state_label=STATE_LABELS[LifecycleState.WAITING_CI],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                gate=diagnosis,
                residual_reason=str(diagnosis["reason"]),
            )

        if diagnosis_state == pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value:
            dependency, marker_note = self._external_dependency_for_failed_check(
                comments, check_identity=pr_gate.REQUIRED_ORDINARY_CI_CONTEXT
            )
            if dependency is not None:
                reason = dependency.reason or str(diagnosis["reason"])
                if marker_note:
                    reason = f"{reason}; {marker_note}"
                owned_gate = dict(diagnosis)
                owned_gate["failure_ownership"] = "PROVEN_CURRENT_MAIN"
                owned_gate["failure_ownership_evidence"] = dependency.evidence
                return PRLifecycle(
                    **base_kwargs,
                    state=LifecycleState.WAITING_EXTERNAL_DEPENDENCY,
                    state_label=STATE_LABELS[LifecycleState.WAITING_EXTERNAL_DEPENDENCY],
                    review_class=review_class,
                    review_verdict=verdict,
                    reviewed_head=reviewed_head,
                    active_leases=lease_payload,
                    local_work=local_payload,
                    gate=owned_gate,
                    external_dependency=dependency.json(),
                    residual_reason=reason,
                    human_action=external_dependency_human_action(dependency),
                )

            ownership = parse_ci_failure_ownership(
                comments, current_head=head, check_identity=pr_gate.REQUIRED_ORDINARY_CI_CONTEXT
            )
            owned_gate = dict(diagnosis)
            owned_gate["failure_ownership"] = ownership["classification"]
            owned_gate["failure_ownership_evidence"] = ownership["evidence"]
            classification = ownership["classification"]
            if classification == "PR_OWNED":
                residual = "CI FAILURE — PR OWNED — exact-head evidence proves this candidate owns the failure; brokered fix is eligible"
                if marker_note:
                    residual = f"{residual}; {marker_note}"
                return PRLifecycle(
                    **base_kwargs,
                    state=LifecycleState.CHANGES_REQUESTED,
                    state_label=STATE_LABELS[LifecycleState.CHANGES_REQUESTED],
                    review_class=review_class,
                    review_verdict=verdict,
                    reviewed_head=reviewed_head,
                    active_leases=lease_payload,
                    local_work=local_payload,
                    gate=owned_gate,
                    residual_reason=residual,
                )
            if classification == "INFRASTRUCTURE":
                residual = "CI FAILURE — INFRASTRUCTURE — workflow/infrastructure repair is owned outside semantic Implementation"
            elif classification == "PROVEN_CURRENT_MAIN":
                residual = "CI FAILURE — MAIN OWNED — durable baseline ownership must be attached before candidate mutation"
            else:
                residual = "CI FAILURE — AMBIGUOUS — ownership must be proven before any semantic branch mutation"
            if marker_note:
                residual = f"{residual}; {marker_note}"
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_PASSED,
                state_label=STATE_LABELS[LifecycleState.REVIEW_PASSED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                gate=owned_gate,
                residual_reason=residual,
            )

        if diagnosis_state == pr_gate.GateDiagnosis.HEAD_MOVED.value:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_READY,
                state_label=STATE_LABELS[LifecycleState.REVIEW_READY],
                review_class=review_class,
                active_leases=lease_payload,
                local_work=local_payload,
                gate=diagnosis,
                residual_reason=str(diagnosis["reason"]),
            )

        if diagnosis_state in {
            pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value,
            pr_gate.GateDiagnosis.INFRASTRUCTURE_ERROR.value,
        }:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_PASSED,
                state_label=STATE_LABELS[LifecycleState.REVIEW_PASSED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                gate=diagnosis,
                residual_reason=f"Integration evidence unavailable/stale: {diagnosis['reason']}",
            )

        gate = pr_gate.evaluate_integration_gate(
            current,
            reviewed_head=reviewed_head,
            reviewed_at=str(exact_review.get("submitted_at") or exact_review.get("submittedAt") or ""),
            combined_status=combined,
            workflow_runs=runs,
        )

        integration_leases = active_by_phase.get("integration", [])
        if integration_leases:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.MERGING,
                state_label=STATE_LABELS[LifecycleState.MERGING],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                gate=gate,
                residual_reason="active exact-head Integration lease",
            )
        if not self.integration_authority:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.INTEGRATION_READY,
                state_label=STATE_LABELS[LifecycleState.INTEGRATION_READY],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                gate=gate,
                residual_reason="bounded Integration authority is not explicitly enabled for this dispatcher",
                human_action="authorize an Integration-capable workflow or run Integration separately",
            )
        if not self.integration_capable:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.INTEGRATION_READY,
                state_label=STATE_LABELS[LifecycleState.INTEGRATION_READY],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                local_work=local_payload,
                gate=gate,
                residual_reason="Integration merge capability is unavailable on this host",
                human_action="run an authorized Integration host with GitHub merge capability",
            )
        return PRLifecycle(
            **base_kwargs,
            state=LifecycleState.INTEGRATION_READY,
            state_label=STATE_LABELS[LifecycleState.INTEGRATION_READY],
            review_class=review_class,
            review_verdict=verdict,
            reviewed_head=reviewed_head,
            active_leases=lease_payload,
            local_work=local_payload,
            gate=gate,
            residual_reason="all exact-head gates are green; bounded Integration may proceed",
        )

    def status(self, *, include_closed: bool = False) -> list[PRLifecycle]:
        return [self.inspect(pr) for pr in self.github.list_prs(include_closed=include_closed)]
