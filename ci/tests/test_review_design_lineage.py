import hashlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from review_design_lineage import (  # noqa: E402
    DESIGN_PROVENANCE_SCHEMA,
    SOURCE_POLICY_SCHEMA,
    Challenge,
    ChallengeKey,
    Disposition,
    EnvironmentApplicability,
    Event,
    EventType,
    HumanDecisionProvenance,
    Identity,
    Projection,
    ReviewDisposition,
    SourceClass,
    SourceDisposition,
    SourceUse,
    State,
    active_source_disposition,
    affected_claims_for_source_policy,
    challenge_used,
    consume_identity,
    create_generation,
    cumulative_drift_baseline,
    external_snapshot_contradictions,
    projection_contradictions,
    reconstruct,
    recover_notes,
    recover_snapshot,
    require_challenge_available,
    validate_design_provenance,
    validate_disposition,
    validate_lineage,
    validate_source_policy_registry,
)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def gen(gid, text, predecessor=None):
    return create_generation(
        task_gid="1217613446414340",
        generation_id=gid,
        snapshot=text.encode(),
        predecessor_generation_id=predecessor,
        relevant_repo_baseline="7dd4259efc071473c91a34498692ed8951f5d7fb",
        created_at="2026-08-19T00:00:00Z",
        created_by="Dish Agent: Review",
    )


def approval(record, event_gid="2"):
    delta_sha = sha("complete material delta set")
    ref = f"asana:decision:{record.generation_id}"
    decision_sha = sha(
        f"Marco approved {record.identity.tuple()} with material deltas {delta_sha}"
    )
    decision = HumanDecisionProvenance(
        decision_ref=ref,
        decision_sha256=decision_sha,
        identity=record.identity,
        material_delta_set_sha256=delta_sha,
    )
    event = Event(
        event_gid,
        EventType.MARCO_APPROVED,
        record.identity,
        "2026-08-19T00:01:00Z",
        "Dish Agent",
        material_delta_set_sha256=delta_sha,
        human_decision_ref=ref,
        human_decision_sha256=decision_sha,
    )
    return event, {ref: decision}


def event(record, gid, kind, successor=None):
    if kind is EventType.MARCO_APPROVED:
        approved, _ = approval(record, gid)
        return approved
    return Event(
        gid,
        kind,
        record.identity,
        "2026-08-19T00:01:00Z",
        "Dish Agent",
        successor_generation_id=successor,
    )


def test_asana_design_recovery_uses_review_v2_snapshot():
    g5 = gen("G5", "approved design")
    result = recover_notes(
        design_bearing=True,
        generation=g5,
        generic_preimage=b"wrong",
    )
    assert result.snapshot == b"approved design"
    assert result.identity == g5.identity
    assert result.source == "dish-design-generation:v1"


def test_parallel_snapshot_conflict_surfaces_and_review_v2_wins():
    g5 = gen("G5", "authoritative")
    problems = external_snapshot_contradictions(
        g5,
        b"different",
        "lifecycle-cache",
    )
    assert problems[0].code == "competing-design-snapshot"
    assert recover_snapshot(g5) == b"authoritative"


def test_non_design_preimage_stays_outside_design_lineage():
    result = recover_notes(
        design_bearing=False,
        generic_preimage=b"operational",
    )
    assert (result.source, result.identity, result.snapshot) == (
        "generic-notes-preimage",
        None,
        b"operational",
    )


def test_lifecycle_and_service_consume_exact_identity_without_minting():
    g5 = gen("G5", "dispatch")
    identity = consume_identity(g5)
    assert identity == g5.identity
    assert Identity(*identity.tuple()) == identity


def test_successor_design_stays_in_review_v2_lineage_and_projection_moves():
    g5 = gen("G5", "dispatched")
    approved, decisions = approval(g5)
    h5 = [
        event(g5, "1", EventType.CREATED),
        approved,
        event(g5, "3", EventType.DISPATCHED),
        event(g5, "4", EventType.REOPENED),
        event(g5, "5", EventType.SUPERSEDED, "G6"),
    ]
    g6 = gen("G6", "changed", "G5")
    h6 = [event(g6, "6", EventType.CREATED)]
    states = {
        "G5": reconstruct(g5, h5, decisions),
        "G6": reconstruct(g6, h6),
    }
    assert validate_lineage([g5, g6], states) == ()
    pointer = Projection(g6.identity, "record-g6", State.AUTHORING, "6")
    assert projection_contradictions(
        g6,
        states["G6"],
        pointer,
        "record-g6",
    ) == ()


def test_replacement_author_and_reviewer_do_not_reset_challenge_budget():
    g5 = gen("G5", "candidate")
    key = ChallengeKey(g5.identity, "blocker", sha("evidence-set"))
    challenge = Challenge(key, "replacement-author", sha("challenge"))
    assert challenge_used(key, [challenge])
    with pytest.raises(ValueError, match="challenge budget"):
        require_challenge_available(key, [challenge])
    validate_disposition(
        challenge,
        ReviewDisposition(key, "replacement-reviewer", Disposition.NARROWS),
        ["original-author", "replacement-author"],
    )


def test_material_author_cannot_self_clear_candidate():
    g5 = gen("G5", "candidate")
    key = ChallengeKey(g5.identity, "blocker", sha("evidence-set"))
    challenge = Challenge(key, "author-b", sha("challenge"))
    with pytest.raises(ValueError, match="cannot independently clear"):
        validate_disposition(
            challenge,
            ReviewDisposition(key, "author-b", Disposition.WITHDRAWS),
            ["author-a", "author-b"],
        )


def test_challenger_cannot_supply_independent_reviewer_disposition():
    g5 = gen("G5", "candidate")
    key = ChallengeKey(g5.identity, "blocker", sha("evidence-set"))
    challenge = Challenge(key, "challenger-only", sha("challenge"))
    with pytest.raises(ValueError, match="challenger cannot supply"):
        validate_disposition(
            challenge,
            ReviewDisposition(key, "challenger-only", Disposition.UPHOLDS),
            ["material-author"],
        )


def test_invalid_foreign_event_does_not_hide_later_valid_created_event():
    g5 = gen("G5", "candidate")
    other = gen("G6", "other")
    state = reconstruct(
        g5,
        [
            event(other, "0", EventType.CREATED),
            event(g5, "1", EventType.CREATED),
        ],
    )
    assert state.state is State.AUTHORING
    assert state.valid_event_gids == ("1",)
    assert any(c.code == "identity-mismatch" for c in state.contradictions)


def test_marco_approval_requires_material_delta_set_digest():
    g5 = gen("G5", "candidate")
    with pytest.raises(ValueError, match="material_delta_set_sha256"):
        Event(
            "2",
            EventType.MARCO_APPROVED,
            g5.identity,
            "2026-08-19T00:01:00Z",
            "Moinudin",
        )


def test_actor_only_approval_cannot_count_but_exact_human_provenance_can():
    g5 = gen("G5", "candidate")
    delta_sha = sha("complete material delta set")
    actor_only = Event(
        "2",
        EventType.MARCO_APPROVED,
        g5.identity,
        "2026-08-19T00:01:00Z",
        "Marco",
        material_delta_set_sha256=delta_sha,
    )
    actor_only_state = reconstruct(
        g5,
        [event(g5, "1", EventType.CREATED), actor_only],
    )
    assert actor_only_state.state is State.AUTHORING
    assert "2" not in actor_only_state.valid_event_gids
    assert any(
        c.code == "invalid-marco-approval-provenance"
        for c in actor_only_state.contradictions
    )
    assert cumulative_drift_baseline(
        "G5",
        [g5],
        {"G5": [event(g5, "1", EventType.CREATED), actor_only]},
    ) is None

    approved, decisions = approval(g5)
    bound_state = reconstruct(
        g5,
        [event(g5, "1", EventType.CREATED), approved],
        decisions,
    )
    assert bound_state.state is State.MARCO_APPROVED
    assert bound_state.contradictions == ()
    assert cumulative_drift_baseline(
        "G5",
        [g5],
        {"G5": [event(g5, "1", EventType.CREATED), approved]},
        decisions,
    ) == g5


def test_recovered_human_decision_must_match_exact_identity_and_delta_set():
    g5 = gen("G5", "candidate")
    approved, decisions = approval(g5)
    ref = approved.human_decision_ref
    assert ref is not None
    bad = HumanDecisionProvenance(
        decision_ref=ref,
        decision_sha256=approved.human_decision_sha256,
        identity=gen("G6", "other").identity,
        material_delta_set_sha256=approved.material_delta_set_sha256,
    )
    state = reconstruct(
        g5,
        [event(g5, "1", EventType.CREATED), approved],
        {ref: bad},
    )
    assert state.state is State.AUTHORING
    assert any(
        c.code == "invalid-marco-approval-provenance"
        for c in state.contradictions
    )


def test_invalid_events_and_pointer_disagreement_are_contradictions():
    g5 = gen("G5", "candidate")
    state = reconstruct(
        g5,
        [
            event(g5, "1", EventType.CREATED),
            event(g5, "2", EventType.DISPATCHED),
        ],
    )
    assert state.state is State.AUTHORING
    assert any(c.code == "invalid-transition" for c in state.contradictions)
    other = gen("G6", "other", "G5")
    pointer = Projection(other.identity, "wrong", State.DISPATCHED, "wrong")
    assert {
        c.code
        for c in projection_contradictions(g5, state, pointer, "record-g5")
    } == {
        "projection-identity-mismatch",
        "projection-record-mismatch",
        "projection-state-mismatch",
        "projection-event-mismatch",
    }


def test_dispatched_successor_without_reopen_is_surfaced():
    g5 = gen("G5", "dispatched")
    approved, decisions = approval(g5)
    state = reconstruct(
        g5,
        [
            event(g5, "1", EventType.CREATED),
            approved,
            event(g5, "3", EventType.DISPATCHED),
        ],
        decisions,
    )
    g6 = gen("G6", "changed", "G5")
    problems = validate_lineage([g5, g6], {"G5": state})
    assert any(
        c.code == "dispatched-successor-without-reopen"
        for c in problems
    )


def test_superseded_requires_successor_identity():
    g5 = gen("G5", "candidate")
    with pytest.raises(ValueError, match="successor_generation_id"):
        Event(
            "5",
            EventType.SUPERSEDED,
            g5.identity,
            "2026-08-19T00:01:00Z",
            "Dish Agent",
        )


def test_superseded_event_successor_mismatch_is_surfaced():
    g5 = gen("G5", "candidate")
    g6 = gen("G6", "actual child", "G5")
    g5_state = reconstruct(
        g5,
        [
            event(g5, "1", EventType.CREATED),
            event(g5, "5", EventType.SUPERSEDED, "G7"),
        ],
    )
    problems = validate_lineage([g5, g6], {"G5": g5_state})
    assert any(
        c.code == "superseded-successor-mismatch"
        and "G7" in c.message
        and "G6" in c.message
        for c in problems
    )


def test_superseded_event_successor_matches_actual_lineage():
    g5 = gen("G5", "candidate")
    g6 = gen("G6", "actual child", "G5")
    g5_state = reconstruct(
        g5,
        [
            event(g5, "1", EventType.CREATED),
            event(g5, "5", EventType.SUPERSEDED, "G6"),
        ],
    )
    assert validate_lineage([g5, g6], {"G5": g5_state}) == ()


def test_cumulative_drift_uses_last_exact_marco_approved_ancestor():
    g4 = gen("G4", "approved")
    g5 = gen("G5", "delta1", "G4")
    g6 = gen("G6", "delta2", "G5")
    approved, decisions = approval(g4)
    histories = {
        "G4": [
            event(g4, "1", EventType.CREATED),
            approved,
        ],
        "G5": [event(g5, "3", EventType.CREATED)],
        "G6": [event(g6, "4", EventType.CREATED)],
    }
    assert cumulative_drift_baseline(
        "G6",
        [g4, g5, g6],
        histories,
        decisions,
    ) == g4


def policy_authority(decision, *, authority_type="MARCO_EXPLICIT"):
    return {
        "authority_type": authority_type,
        "durable_ref": f"asana:decision:{sha(decision)[:12]}",
        "decided_by": "Marco",
        "decision": decision,
        "decision_sha256": sha(decision),
        "effective_at": "2026-08-20T00:00:00Z",
    }


def source_policy(*events):
    return {
        "schema": SOURCE_POLICY_SCHEMA,
        "schema_version": 1,
        "sources": [
            {
                "source_id": "company-x",
                "organization": "Company X",
                "primary_source": {
                    "title": "Primary X",
                    "uri": "https://example.invalid/x",
                    "version_or_date": "2026-08-20",
                },
            },
            {
                "source_id": "company-y",
                "organization": "Company Y",
                "primary_source": {
                    "title": "Primary Y",
                    "uri": "https://example.invalid/y",
                    "version_or_date": "2026-08-20",
                },
            },
        ],
        "disposition_events": list(events),
    }


def policy_event(
    event_id,
    source_id,
    decision_class,
    disposition,
    *,
    predecessor=None,
    authority_type="MARCO_EXPLICIT",
):
    decision = f"{source_id} {disposition} for {decision_class}"
    return {
        "event_id": event_id,
        "source_id": source_id,
        "decision_class": decision_class,
        "disposition": disposition,
        "predecessor_event_id": predecessor,
        "authority": policy_authority(decision, authority_type=authority_type),
    }


def external_support(
    support_id,
    source_id,
    *,
    use="NORMATIVE",
    decision_class="approval-ux",
    observed="NO_ACTIVE_DISPOSITION",
    event_id=None,
    caution=None,
):
    support = {
        "support_id": support_id,
        "source_class": SourceClass.EXTERNAL_PRIMARY_EVIDENCE.value,
        "evidence_refs": [f"primary:{source_id}"],
        "source_id": source_id,
        "source_use": use,
        "decision_class": decision_class,
        "source_statement": f"{source_id} primary source says the bounded thing",
        "dish_inference": "Dish adapts the bounded source statement to this claim",
    }
    if use == SourceUse.NORMATIVE.value:
        support["source_policy"] = {
            "observed_disposition": observed,
            "event_id": event_id,
        }
    if caution is not None:
        support["caution_acknowledgement"] = caution
    return support


def local_support(support_id, source_class=SourceClass.DISH_LOCAL_INFERENCE.value):
    return {
        "support_id": support_id,
        "source_class": source_class,
        "evidence_refs": [f"dish:{support_id}"],
    }


def provenance(record, supports, *, mechanisms=()):
    return {
        "schema": DESIGN_PROVENANCE_SCHEMA,
        "task_gid": record.identity.task_gid,
        "generation_id": record.identity.generation_id,
        "canonical_sha256": record.identity.canonical_sha256,
        "relevant_repo_baseline": record.identity.relevant_repo_baseline,
        "claims": [
            {
                "claim_id": "claim-approval",
                "decision": "Use bounded approval mechanism",
                "problem_outcome": "Keep the control understandable and reversible",
                "operator_cost": "One explicit approval at the material boundary",
                "failure_mode": "Unsupported gate would add needless ceremony",
                "alternatives_considered": ["No new mandatory gate"],
                "reversibility": "Remove through the existing successor design path",
                "assumptions": [],
                "supports": list(supports),
                "mechanisms": list(mechanisms),
            }
        ],
    }


def environment_requirement(status, *, required=True):
    item = {
        "capability": "control-surface",
        "target_surface": "current-chatgpt-environment",
        "required": required,
        "status": status,
        "refresh_trigger": "product surface or permission change",
    }
    if status != EnvironmentApplicability.UNKNOWN.value:
        item.update(
            {
                "evidence_ref": "runtime:verified-surface",
                "evidence_as_of": "2026-08-20",
            }
        )
    return item


def test_repository_source_policy_registry_is_valid_and_empty_is_not_allowed():
    import json

    registry = json.loads(
        (ROOT / "dish/docs/agents/source-policy.json").read_text()
    )
    assert validate_source_policy_registry(registry) == ()
    assert active_source_disposition(registry, "missing", "approval-ux") is None


def test_source_policy_requires_durable_human_authority_not_account_attribution():
    actor_only = policy_event(
        "x-1",
        "company-x",
        "approval-ux",
        SourceDisposition.DISALLOWED_AS_PRECEDENT.value,
        authority_type="AUTHENTICATED_ACCOUNT",
    )
    problems = validate_source_policy_registry(source_policy(actor_only))
    assert any(p.code == "source-policy-human-authority" for p in problems)


def test_invalid_source_policy_is_reported_without_hiding_claim_validation():
    actor_only = policy_event(
        "x-1",
        "company-x",
        "approval-ux",
        SourceDisposition.DISALLOWED_AS_PRECEDENT.value,
        authority_type="AUTHENTICATED_ACCOUNT",
    )
    registry = source_policy(actor_only)
    g5 = gen("G5-invalid-policy", "candidate")
    support = external_support(
        "x",
        "company-x",
        observed=SourceDisposition.DISALLOWED_AS_PRECEDENT.value,
        event_id="x-1",
    )

    problems = validate_design_provenance(provenance(g5, [support]), g5.identity, registry)

    assert any(p.code == "source-policy-human-authority" for p in problems)


def test_source_policy_supersession_is_append_only_and_terminal_event_wins():
    allowed = policy_event(
        "x-1",
        "company-x",
        "approval-ux",
        SourceDisposition.ALLOWED.value,
    )
    blocked = policy_event(
        "x-2",
        "company-x",
        "approval-ux",
        SourceDisposition.DISALLOWED_AS_PRECEDENT.value,
        predecessor="x-1",
    )
    registry = source_policy(allowed, blocked)

    assert validate_source_policy_registry(registry) == ()
    assert [event["event_id"] for event in registry["disposition_events"]] == ["x-1", "x-2"]
    state = active_source_disposition(registry, "company-x", "approval-ux")
    assert state is not None
    assert state.event_id == "x-2"
    assert state.disposition is SourceDisposition.DISALLOWED_AS_PRECEDENT


def test_normative_use_records_no_active_disposition_instead_of_inventing_allowed():
    registry = source_policy()
    g5 = gen("G5-no-disposition", "candidate")
    support = external_support("x", "company-x")

    assert validate_design_provenance(provenance(g5, [support]), g5.identity, registry) == ()

    support["source_policy"] = {
        "observed_disposition": SourceDisposition.ALLOWED.value,
        "event_id": None,
    }
    problems = validate_design_provenance(provenance(g5, [support]), g5.identity, registry)
    assert any(p.code == "source-policy-stale-or-missing" for p in problems)


def test_scoped_source_disposition_preserves_factual_use_and_other_decision_classes():
    blocked = policy_event(
        "x-1",
        "company-x",
        "approval-ux",
        SourceDisposition.DISALLOWED_AS_PRECEDENT.value,
    )
    registry = source_policy(blocked)
    assert active_source_disposition(registry, "company-x", "approval-ux").disposition is SourceDisposition.DISALLOWED_AS_PRECEDENT
    assert active_source_disposition(registry, "company-x", "rollback") is None

    g5 = gen("G5-source-policy", "candidate")
    factual = external_support(
        "x-fact",
        "company-x",
        use=SourceUse.FACTUAL.value,
        decision_class="approval-ux",
    )
    assert validate_design_provenance(provenance(g5, [factual]), g5.identity, registry) == ()


def test_disallowed_normative_precedent_is_rejected_but_local_inference_is_valid():
    blocked = policy_event(
        "x-1",
        "company-x",
        "approval-ux",
        SourceDisposition.DISALLOWED_AS_PRECEDENT.value,
    )
    registry = source_policy(blocked)
    g5 = gen("G5-disallowed", "candidate")
    x = external_support(
        "x-normative",
        "company-x",
        observed=SourceDisposition.DISALLOWED_AS_PRECEDENT.value,
        event_id="x-1",
    )
    problems = validate_design_provenance(provenance(g5, [x]), g5.identity, registry)
    assert any(p.code == "disallowed-normative-source" for p in problems)

    local = local_support("dish-inference")
    assert validate_design_provenance(provenance(g5, [local]), g5.identity, registry) == ()


def test_caution_must_be_acknowledged_and_policy_snapshot_must_match_current():
    caution = policy_event(
        "x-1",
        "company-x",
        "approval-ux",
        SourceDisposition.CAUTION.value,
    )
    registry = source_policy(caution)
    g5 = gen("G5-caution", "candidate")
    support = external_support(
        "x-normative",
        "company-x",
        observed=SourceDisposition.CAUTION.value,
        event_id="x-1",
    )
    problems = validate_design_provenance(provenance(g5, [support]), g5.identity, registry)
    assert any(p.code == "source-caution-unacknowledged" for p in problems)

    support["caution_acknowledgement"] = "Treat as comparator only; independent Dish evidence carries the choice."
    assert validate_design_provenance(provenance(g5, [support]), g5.identity, registry) == ()


def test_required_unknown_or_unavailable_cannot_be_recommended():
    registry = source_policy()
    g5 = gen("G5-env", "candidate")
    local = local_support("dish-inference")
    for status, code in (
        (EnvironmentApplicability.UNKNOWN.value, "required-environment-unknown"),
        (
            EnvironmentApplicability.VERIFIED_UNAVAILABLE.value,
            "required-environment-unavailable",
        ),
    ):
        record = provenance(
            g5,
            [local],
            mechanisms=[
                {
                    "mechanism_id": f"mechanism-{status}",
                    "recommended": True,
                    "requirements": [environment_requirement(status)],
                }
            ],
        )
        problems = validate_design_provenance(record, g5.identity, registry)
        assert any(p.code == code for p in problems)

    candidate = provenance(
        g5,
        [local],
        mechanisms=[
            {
                "mechanism_id": "candidate-unknown",
                "recommended": False,
                "requirements": [
                    environment_requirement(EnvironmentApplicability.UNKNOWN.value)
                ],
            }
        ],
    )
    assert validate_design_provenance(candidate, g5.identity, registry) == ()


def test_source_policy_change_flags_only_current_exact_claim_and_preserves_independent_support():
    g5 = gen("G5-current", "current")
    historical = gen("G4-historical", "historical")
    x = external_support("x-approval", "company-x")
    y = external_support(
        "y-rollback",
        "company-y",
        decision_class="rollback",
    )
    incident = local_support("incident", SourceClass.DISH_INCIDENT_EVIDENCE.value)
    current_record = provenance(g5, [x, y, incident])
    historical_record = provenance(historical, [x])

    affected = affected_claims_for_source_policy(
        [historical_record, current_record],
        {g5.identity.task_gid: g5.identity},
        source_id="company-x",
        decision_class="approval-ux",
    )
    assert len(affected) == 1
    assert affected[0].generation_id == g5.generation_id
    assert affected[0].support_id == "x-approval"
    assert affected[0].has_independent_support is True


def test_provenance_binding_prevents_historical_generation_from_becoming_current():
    registry = source_policy()
    g5 = gen("G5-binding", "current")
    g4 = gen("G4-binding", "old")
    record = provenance(g4, [local_support("dish")])
    problems = validate_design_provenance(record, g5.identity, registry)
    assert any(p.code == "design-provenance-identity-mismatch" for p in problems)


def test_source_statement_and_dish_inference_are_separate_required_fields():
    registry = source_policy()
    g5 = gen("G5-separation", "candidate")
    support = external_support("x", "company-x")
    support["source_statement"] = ""
    support["dish_inference"] = ""
    problems = validate_design_provenance(provenance(g5, [support]), g5.identity, registry)
    assert {p.code for p in problems} >= {
        "support-source-statement",
        "support-dish-inference",
    }
