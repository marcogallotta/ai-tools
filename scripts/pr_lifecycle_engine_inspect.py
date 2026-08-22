"""Lifecycle classification from durable GitHub/Asana state."""
from __future__ import annotations

from pr_lifecycle_support import *
from pr_lifecycle_helpers import *
from installed_host_cert import EVIDENCE as INSTALLED_HOST_CERT_EVIDENCE, requirement_for_files, status_from_comments
from pr_lifecycle_helpers import (
    _integration_order_reason, _lease_json, _mergeability_reason,
    _pr_base, _pr_branch, _pr_number, _pr_title, _pr_url,
    _parse_time, _reviewed_head, _utcnow,
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
    ) -> None:
        self.github = github
        self.asana = asana
        self.integration_authority = integration_authority
        self.integration_capable = integration_capable
        self.merge_method = merge_method
        self.now = now

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
    ) -> tuple[ExternalDependency | None, str | None, bool]:
        try:
            record = parse_external_dependency(comments)
        except LifecycleError as exc:
            return None, f"external dependency marker invalid: {exc}", False
        if record is None or record.action == "resolved" or record.check != check_identity:
            return None, None, False

        status_reason = record.reason
        if record.owner_pr is not None:
            try:
                owner = self.github.get_pr(record.owner_pr)
            except (LifecycleError, AssertionError):
                return None, f"external owner PR #{record.owner_pr} authority read failed", False
            if bool(owner.get("merged") or owner.get("merged_at")):
                return None, f"external owner PR #{record.owner_pr} is merged; re-evaluate exact-head evidence", False
            owner_state = pr_gate.pr_state(owner)
            if owner_state != "open":
                status_reason = (
                    f"external owner PR #{record.owner_pr} is closed unmerged; repair continuation is required"
                )
                return record, status_reason, False
        elif self.asana is not None:
            try:
                owner_task = self.asana.get_task(record.task_gid)
            except LifecycleError:
                return None, f"external owner task {record.task_gid} authority read failed", False
            if bool(owner_task.get("completed")):
                return None, f"external owner task {record.task_gid} is complete; re-evaluate exact-head evidence", False
        else:
            return None, f"external owner task {record.task_gid} authority is unavailable", False

        return record, status_reason, True


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

        host_requirement = None
        host_cert_status = None
        get_pr_files = getattr(self.github, "get_pr_files", None)
        if callable(get_pr_files):
            host_requirement = requirement_for_files(get_pr_files(number))
            if host_requirement is not None:
                host_cert_status = status_from_comments(
                    comments,
                    repository=self.github.repository,
                    pr_number=number,
                    branch=base_kwargs["branch"],
                    head=head,
                    task_ids=task_ids,
                    requirement=host_requirement,
                )

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

        if host_requirement is not None and host_cert_status is not None and not host_cert_status.passed:
            lifecycle = implementation_continuation_lifecycle(
                base_kwargs=base_kwargs,
                evidence=INSTALLED_HOST_CERT_EVIDENCE,
                review_class=review_class,
                lease_payload=lease_payload,
                implementation_active=bool(active_by_phase.get("implementation")),
            )
            lifecycle.residual_reason = (
                "hook/config/install-wiring candidate requires exact-head installed-host Implementation evidence: "
                + str(host_cert_status.error or "certificate missing")
            )
            lifecycle.human_action = None
            return lifecycle

        if draft:
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

        review_metadata = review_gate_metadata(exact_review)
        base_kwargs["post_merge_gates"] = list(review_metadata.post_merge_gates)
        if review_metadata.error is not None:
            return PRLifecycle(
                **base_kwargs,
                state=LifecycleState.REVIEW_PASSED,
                state_label=STATE_LABELS[LifecycleState.REVIEW_PASSED],
                review_class=review_class,
                review_verdict=verdict,
                reviewed_head=reviewed_head,
                active_leases=lease_payload,
                residual_reason=f"malformed Review phase metadata: {review_metadata.error}",
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
                    gate=diagnosis,
                    residual_reason=(
                        f"{pending_cert.instruction}; exact-head CI pending: {diagnosis['reason']}"
                    ),
                    human_action=pending_cert.instruction if pending_cert.handoff_present else None,
                )
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
            ownership = parse_ci_failure_ownership(
                comments, current_head=head, check_identity=pr_gate.REQUIRED_ORDINARY_CI_CONTEXT
            )
            dependency, marker_note, owner_active = self._external_dependency_for_failed_check(
                comments, check_identity=pr_gate.REQUIRED_ORDINARY_CI_CONTEXT
            )
            if dependency is not None and ownership.get("comment_id") is None:
                ownership = {
                    **ownership,
                    "classification": "PROVEN_CURRENT_MAIN",
                    "evidence": dependency.evidence,
                    "candidate_disposition": "NON_BLOCKING_PROVEN_UNRELATED",
                    "failure_main_sha": dependency.main_sha,
                }
            expected_generation = (
                f"run-{diagnosis.get('required_workflow_run_id')}-"
                f"attempt-{diagnosis.get('required_workflow_run_attempt')}"
            )
            if (
                ownership.get("evidence_generation") is not None
                and ownership.get("evidence_generation") != expected_generation
            ):
                ownership = {
                    "classification": "AMBIGUOUS",
                    "candidate_disposition": "BLOCKING",
                    "evidence": "CI ownership marker belongs to a stale workflow evidence generation",
                    "comment_id": ownership.get("comment_id"),
                }
            ownership_time = _parse_time(ownership.get("ownership_observed_at"))
            if dependency is not None and dependency.candidate_head not in {None, head.lower()}:
                dependency = None
                owner_active = False
                marker_note = "external dependency marker belongs to an older candidate head"
            if (
                dependency is not None and ownership_time is not None
                and ownership_time > dependency.timestamp
                and ownership["classification"] in {"PR_OWNED", "AMBIGUOUS"}
            ):
                dependency = None
                owner_active = False
                marker_note = "newer exact-head ownership evidence overrides the stale baseline marker"

            if ownership["classification"] == "LIKELY_NON_PR_OWNED" and not owner_active:
                owner_gid = str(ownership.get("repair_owner_task") or "")
                try:
                    owner_task = self.asana.get_task(owner_gid) if self.asana is not None else None
                except LifecycleError:
                    owner_task = None
                    marker_note = f"repair owner task {owner_gid} authority read failed"
                if owner_task is not None and not bool(owner_task.get("completed")):
                    owner_active = True
                else:
                    ownership = {
                        "classification": "AMBIGUOUS",
                        "candidate_disposition": "BLOCKING",
                        "evidence": marker_note or f"repair owner task {owner_gid} is not active",
                        "comment_id": ownership.get("comment_id"),
                    }

            owned_gate = dict(diagnosis)
            owned_gate["raw_gate_outcome"] = "FAILED"
            owned_gate["failure_ownership"] = ownership["classification"]
            owned_gate["failure_ownership_evidence"] = ownership["evidence"]
            owned_gate["candidate_disposition"] = ownership.get("candidate_disposition") or "BLOCKING"
            owned_gate["repair_owner_active"] = bool(owner_active)
            for key in (
                "candidate_disposition", "ownership_observed_at", "evidence_generation",
                "causal_basis", "contrary_evidence", "repair_owner_task", "failure_main_sha",
                "main_reproduction_evidence", "candidate_specific_evidence",
                "semantic_interaction", "interaction_hypothesis", "targeted_interaction_evidence",
            ):
                if ownership.get(key) is not None:
                    owned_gate[key] = ownership[key]
            if ownership.get("causal_fingerprint"):
                owned_gate["failure_causal_fingerprint"] = ownership["causal_fingerprint"]
                owned_gate["failure_causal_identity"] = ownership["causal_identity"]
            classification = ownership["classification"]
            owned_gate["reconciliation_kind"] = {
                "PR_OWNED": "pr-owned-fix",
                "LIKELY_NON_PR_OWNED": "non-blocking-likely-unrelated",
                "PROVEN_CURRENT_MAIN": "current-main-corrective-owner",
                "INFRASTRUCTURE": "infrastructure-retry",
                "AMBIGUOUS": "ownership-required",
            }.get(classification, "ownership-required")

            baseline_admitted = (
                ownership.get("candidate_disposition") == "MERGEABLE_WITH_BASELINE_DEBT"
                and pending_cert is None
                and dependency is not None and owner_active
                and dependency.candidate_head == head.lower()
                and dependency.task_gid == ownership.get("repair_owner_task")
                and dependency.main_sha == ownership.get("failure_main_sha")
                and dependency.causal_fingerprint == ownership.get("causal_fingerprint")
            )
            if baseline_admitted:
                owned_gate["reconciliation_kind"] = "baseline-debt-integration"
                return PRLifecycle(
                    **base_kwargs,
                    state=LifecycleState.INTEGRATION_READY,
                    state_label=STATE_LABELS[LifecycleState.INTEGRATION_READY],
                    review_class=review_class,
                    review_verdict=verdict,
                    reviewed_head=reviewed_head,
                    active_leases=lease_payload,
                    local_work=local_payload,
                    gate=owned_gate,
                    external_dependency=dependency.json(),
                    residual_reason=(
                        "required CI remains failed, but exact current-main reproduction, candidate-specific "
                        "evidence, semantic non-interaction, exact-head Review, and the active repair owner "
                        "mechanically admit landing with baseline debt"
                    ),
                )
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
            if classification == "LIKELY_NON_PR_OWNED":
                residual = (
                    "CI FAILURE — LIKELY NOT PR OWNED — raw failure remains visible; lifecycle may continue "
                    "without a duplicate rerun, but unresolved-red final landing is not admitted"
                )
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
                    external_dependency=dependency.json() if dependency is not None else None,
                    residual_reason=residual,
                )
            if dependency is not None and classification == "PROVEN_CURRENT_MAIN":
                reason = dependency.reason or str(diagnosis["reason"])
                if marker_note:
                    reason = f"{reason}; {marker_note}"
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
                    human_action=(
                        external_dependency_human_action(dependency)
                        if owner_active
                        else "Send a fix agent to restore the baseline repair owner."
                    ),
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
                residual_reason="bounded local Integration authority is not explicitly enabled for this dispatcher",
                human_action="run the repository lifecycle on an authorized local Integration controller",
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
                residual_reason=(
                    "local Git-capable Integration launcher is unavailable on this host; V1-A has no "
                    "remote/connector/broker landing fallback"
                ),
                human_action=None,
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
            residual_reason="all exact-head gates are green; durable local Integration handoff may proceed",
        )

    def status(self, *, include_closed: bool = False) -> list[PRLifecycle]:
        return [self.inspect(pr) for pr in self.github.list_prs(include_closed=include_closed)]
