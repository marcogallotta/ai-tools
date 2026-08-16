from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import uuid
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("pr_mutation_broker", SCRIPTS / "pr_mutation_broker.py")
assert SPEC and SPEC.loader
broker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = broker
SPEC.loader.exec_module(broker)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
MAIN = "b" * 40
REPO = "marcogallotta/ai-tools"
REPO_ID = 1304888921
TASK = "1217486212354554"
ROUTE = "worker-fix"


def request_comment(
    *,
    comment_id=10,
    action="fix",
    head=HEAD,
    review=77,
    grant=None,
    generation=None,
    route=ROUTE,
    authority=None,
    branch="agent/integration-v1",
):
    marker = broker.request_marker(
        request_id=str(uuid.UUID(int=comment_id)),
        action=action,
        task_gid=TASK,
        pr_number=95,
        branch=branch,
        head=head,
        review_id=review,
        main_sha=MAIN if action in {"merge", "integration-reconcile"} else None,
        grant_id=grant,
        generation=generation,
        route=route,
        authority_id=authority,
    )
    return {
        "id": comment_id,
        "body": marker + "\nrequest",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "user": {"login": "agent-user"},
    }


def event_for(*, comment_id=501, kind="grant", action="fix", grant_id=None, generation=1, run_id=900, attempt=1, route=ROUTE, request=None):
    request = request or broker.parse_request_comment(request_comment(action=action if kind == "grant" else "fix"))
    grant_id = grant_id or str(uuid.UUID(int=999))
    payload = broker.make_event_payload(
        repository=REPO,
        repository_id=REPO_ID,
        kind=kind,
        request=request,
        grant_id=grant_id,
        generation=generation,
        action=action,
        branch=request.branch,
        starting_head=request.head,
        review_id=request.review_id,
        main_sha=request.main_sha,
        route=route,
        run_id=run_id,
        run_attempt=attempt,
        trusted_source_sha=MAIN,
        issued_at=NOW,
        stale_after=NOW + timedelta(minutes=60),
        event_id=str(uuid.UUID(int=comment_id + 1000)),
        outcome="closed" if kind == "close" else "accepted",
    )
    return broker.event_from_payload(payload, comment_id=comment_id)


class FakeProofGitHub:
    repository = REPO

    def __init__(self, event, *, expired=False, duplicate=False, conclusion="success", run_attempt=None):
        self.event = event
        self.run_attempt = run_attempt or int(event.payload["run_attempt"])
        proof = broker.proof_payload(event)
        self.archive = broker.proof_zip_bytes(proof)
        digest = "sha256:" + hashlib.sha256(self.archive).hexdigest()
        self.complete = broker.BrokerEvent(
            payload=event.payload,
            event_digest=event.event_digest,
            comment_id=event.comment_id,
            proof_state="COMPLETE",
            artifact_id=7001,
            artifact_digest=digest,
        )
        self.run = {
            "id": int(event.payload["run_id"]),
            "run_attempt": self.run_attempt,
            "event": "issue_comment",
            "conclusion": conclusion,
            "path": broker.WORKFLOW_PATH,
            "head_sha": event.payload["trusted_source_sha"],
            "repository": {"id": REPO_ID},
        }
        artifact = {
            "id": 7001,
            "name": broker.artifact_name(event.payload["run_id"], event.payload["run_attempt"], event.comment_id),
            "expired": expired,
            "digest": digest,
            "workflow_run": {"id": int(event.payload["run_id"])},
        }
        self.artifacts = [artifact, deepcopy(artifact)] if duplicate else [artifact]

    def get_workflow_run_attempt(self, run_id, attempt):
        assert run_id == int(self.event.payload["run_id"])
        assert attempt == int(self.event.payload["run_attempt"])
        return deepcopy(self.run)

    def get_run_artifacts(self, run_id):
        return deepcopy(self.artifacts)

    def download_artifact(self, artifact_id):
        assert artifact_id == 7001
        return self.archive


def completed_comment(fake, *, comment_id=None, event=None):
    event = event or fake.event
    return {
        "id": fake.event.comment_id if comment_id is None else comment_id,
        "body": broker.complete_event_comment(
            event, artifact_id=fake.complete.artifact_id, artifact_digest=fake.complete.artifact_digest
        ),
        "created_at": NOW.isoformat(),
    }


def test_unrelated_comment_never_enters_broker_filter():
    event = {"issue": {"number": 95, "pull_request": {"url": "x"}}, "comment": {"id": 1, "body": "ordinary comment"}}
    assert broker.broker_filter_event(event) == (False, None, None)


def test_filter_requires_one_valid_request_for_same_pr():
    comment = request_comment()
    event = {"issue": {"number": 95, "pull_request": {"url": "x"}}, "comment": comment}
    assert broker.broker_filter_event(event) == (True, 95, 10)
    event["comment"]["body"] += "\n" + comment["body"]
    assert broker.broker_filter_event(event) == (False, None, None)


def test_request_digest_changes_when_comment_payload_is_edited():
    first = broker.parse_request_comment(request_comment())
    edited = request_comment()
    edited["body"] += " edited"
    second = broker.parse_request_comment(edited)
    assert first.comment_digest != second.comment_digest


def test_provisional_event_is_non_authoritative():
    event = event_for()
    fake = FakeProofGitHub(event)
    with pytest.raises(broker.BrokerProofError, match="incomplete"):
        broker.verify_event_proof(fake, event, expected_repository=REPO, expected_repository_id=REPO_ID)


def test_legitimate_exact_run_attempt_comment_and_artifact_verify():
    event = event_for()
    fake = FakeProofGitHub(event)
    parsed = broker.parse_event_comment(completed_comment(fake))
    assert parsed is not None
    verified = broker.verify_event_proof(fake, parsed, expected_repository=REPO, expected_repository_id=REPO_ID)
    assert verified.comment_id == event.comment_id
    assert verified.event_digest == event.event_digest


def test_forged_event_copying_real_run_id_is_rejected_by_comment_bound_artifact():
    event = event_for(comment_id=501)
    fake = FakeProofGitHub(event)
    forged = broker.parse_event_comment(completed_comment(fake, comment_id=777))
    assert forged is not None
    with pytest.raises(broker.BrokerProofError, match="missing, expired, or duplicated"):
        broker.verify_event_proof(fake, forged, expected_repository=REPO, expected_repository_id=REPO_ID)


def test_copying_entire_legitimate_comment_to_new_comment_id_is_rejected():
    event = event_for(comment_id=501)
    fake = FakeProofGitHub(event)
    copied = completed_comment(fake, comment_id=502)
    parsed = broker.parse_event_comment(copied)
    assert parsed is not None and parsed.comment_id == 502
    with pytest.raises(broker.BrokerProofError):
        broker.verify_event_proof(fake, parsed, expected_repository=REPO, expected_repository_id=REPO_ID)


def test_editing_original_event_payload_breaks_digest_before_artifact_lookup():
    event = event_for()
    fake = FakeProofGitHub(event)
    body = completed_comment(fake)["body"]
    marker = broker._event_marker_fields(body)
    assert marker
    payload = broker._decode_payload(marker["payload"])
    payload["route"] = "forged-route"
    altered_token = broker._payload_token(payload)
    tampered = body.replace(marker["payload"], altered_token)
    with pytest.raises(broker.BrokerProofError, match="event digest mismatch"):
        broker.parse_event_comment({"id": event.comment_id, "body": tampered})


@pytest.mark.parametrize("kind", ["renew", "close", "takeover"])
def test_forged_liveness_or_close_events_require_their_own_exact_comment_proof(kind):
    original = event_for(comment_id=501)
    fake = FakeProofGitHub(original)
    request = broker.parse_request_comment(request_comment(comment_id=20, action="renew", grant=original.grant_id, generation=1))
    forged = event_for(
        comment_id=600,
        kind=kind,
        action="fix",
        grant_id=original.grant_id,
        generation=1 if kind != "takeover" else 2,
        request=request,
    )
    # Reuse the legitimate run metadata/artifact id/digest, but there is no artifact
    # bound to forged comment 600.
    forged_complete = broker.BrokerEvent(
        payload=forged.payload,
        event_digest=forged.event_digest,
        comment_id=forged.comment_id,
        proof_state="COMPLETE",
        artifact_id=fake.complete.artifact_id,
        artifact_digest=fake.complete.artifact_digest,
    )
    with pytest.raises(broker.BrokerProofError):
        broker.verify_event_proof(fake, forged_complete, expected_repository=REPO, expected_repository_id=REPO_ID)


def test_rerun_attempt_cannot_reinterpret_old_grant():
    event = event_for(attempt=1)
    fake = FakeProofGitHub(event, run_attempt=2)
    parsed = broker.BrokerEvent(
        payload=event.payload,
        event_digest=event.event_digest,
        comment_id=event.comment_id,
        proof_state="COMPLETE",
        artifact_id=fake.complete.artifact_id,
        artifact_digest=fake.complete.artifact_digest,
    )
    with pytest.raises(broker.BrokerProofError, match="run attempt identity mismatch"):
        broker.verify_event_proof(fake, parsed, expected_repository=REPO, expected_repository_id=REPO_ID)


@pytest.mark.parametrize("expired,duplicate", [(True, False), (False, True)])
def test_missing_expired_or_duplicate_current_proof_fails_closed(expired, duplicate):
    event = event_for()
    fake = FakeProofGitHub(event, expired=expired, duplicate=duplicate)
    with pytest.raises(broker.BrokerProofError, match="RECOVERY REQUIRED"):
        broker.verify_event_proof(fake, fake.complete, expected_repository=REPO, expected_repository_id=REPO_ID)


def test_one_active_mutation_grant_per_pr_and_no_age_only_transfer():
    first = event_for(comment_id=501)
    second = event_for(comment_id=502, grant_id=str(uuid.UUID(int=1001)), generation=2)
    first_complete = broker.BrokerEvent(first.payload, first.event_digest, first.comment_id, "COMPLETE", 1, "sha256:" + "0" * 64)
    second_complete = broker.BrokerEvent(second.payload, second.event_digest, second.comment_id, "COMPLETE", 2, "sha256:" + "1" * 64)
    with pytest.raises(broker.BrokerProofError, match="advanced without proven takeover"):
        broker.fold_verified_events([first_complete, second_complete])
    state = broker.fold_verified_events([first_complete])
    assert state and not state.closed
    stale = broker.replace(state, stale_after=NOW - timedelta(seconds=1))
    new_request = broker.parse_request_comment(request_comment(comment_id=30, action="fix"))
    with pytest.raises(broker.BrokerError, match="BUSY"):
        broker.decision_for_request(new_request, current=stale, task={"gid": TASK, "notes": ""}, now=NOW)


def test_stale_takeover_requires_positive_marco_authority():
    event = event_for()
    state = broker.GrantState(
        grant_id=event.grant_id,
        generation=1,
        action="fix",
        task_gid=TASK,
        pr_number=95,
        branch="agent/integration-v1",
        starting_head=HEAD,
        review_id=77,
        main_sha=None,
        route=ROUTE,
        consumer_id=event.payload["consumer_id"],
        issued_at=NOW - timedelta(hours=2),
        stale_after=NOW - timedelta(hours=1),
        event_comment_id=501,
    )
    request = broker.parse_request_comment(
        request_comment(comment_id=40, action="takeover", grant=state.grant_id, generation=state.generation, authority="decision-1")
    )
    with pytest.raises(broker.BrokerError, match="Marco authority"):
        broker.decision_for_request(request, current=state, task={"gid": TASK, "notes": ""}, now=NOW)
    task = {
        "gid": TASK,
        "notes": "<!-- dish-marco-authority:v1 decision=decision-1 action=takeover broker_admission=true -->",
    }
    kind, grant_id, generation = broker.decision_for_request(request, current=state, task=task, now=NOW)
    assert kind == "takeover"
    assert grant_id != state.grant_id
    assert generation == 2


def stale_grant(*, action="fix", route=ROUTE):
    event = event_for(action=action, route=route)
    return broker.GrantState(
        grant_id=event.grant_id,
        generation=1,
        action=action,
        task_gid=TASK,
        pr_number=95,
        branch="agent/integration-v1",
        starting_head=HEAD,
        review_id=77,
        main_sha=MAIN if action in {"merge", "integration-reconcile"} else None,
        route=route,
        consumer_id=event.payload["consumer_id"],
        issued_at=NOW - timedelta(hours=2),
        stale_after=NOW - timedelta(hours=1),
        event_comment_id=501,
    )


@pytest.mark.parametrize(
    ("grant_id", "generation"),
    [
        (None, 1),
        (str(uuid.UUID(int=1000)), 1),
        (str(uuid.UUID(int=999)), 2),
    ],
)
def test_takeover_rejects_missing_or_wrong_current_grant_generation(grant_id, generation):
    state = stale_grant()
    request = broker.parse_request_comment(
        request_comment(action="takeover", grant=grant_id, generation=generation, authority="decision-1")
    )
    task = {"gid": TASK, "notes": "<!-- dish-marco-authority:v1 decision=decision-1 action=takeover broker_admission=true -->"}
    with pytest.raises(broker.BrokerError, match="exact current grant generation/branch/head"):
        broker.decision_for_request(request, current=state, task=task, now=NOW)


@pytest.mark.parametrize(
    ("head", "branch"),
    [("c" * 40, "agent/integration-v1"), (HEAD, "agent/moved")],
)
def test_takeover_rejects_head_or_branch_movement_before_reclassification_or_recovery(head, branch):
    state = stale_grant()
    request = broker.parse_request_comment(
        request_comment(
            action="takeover",
            head=head,
            branch=branch,
            grant=state.grant_id,
            generation=state.generation,
            authority="decision-1",
        )
    )
    task = {"gid": TASK, "notes": "<!-- dish-marco-authority:v1 decision=decision-1 action=takeover broker_admission=true -->"}
    with pytest.raises(broker.BrokerError, match="exact current grant generation/branch/head"):
        broker.decision_for_request(request, current=state, task=task, now=NOW)


@pytest.mark.parametrize(
    ("action", "route", "policy"),
    [
        ("fix", ROUTE, {ROUTE: {"role": "implementation", "actions": ["takeover"]}}),
    ],
)
def test_takeover_cannot_use_literal_takeover_permission_for_original_action(action, route, policy):
    state = stale_grant(action=action, route=route)
    request = broker.parse_request_comment(
        request_comment(
            action="takeover",
            grant=state.grant_id,
            generation=state.generation,
            route=route,
            authority="decision-1",
        )
    )
    with pytest.raises(broker.BrokerError, match="current grant's standing role/action authority"):
        broker.validate_takeover_preconditions(request, current=state, route_policy=policy)


def test_correctly_bound_marco_authorized_takeover_preserves_original_action_authority():
    state = stale_grant()
    request = broker.parse_request_comment(
        request_comment(
            action="takeover",
            grant=state.grant_id,
            generation=state.generation,
            authority="decision-1",
        )
    )
    broker.validate_takeover_preconditions(
        request,
        current=state,
        route_policy={ROUTE: {"role": "implementation", "actions": ["fix"]}},
    )
    task = {"gid": TASK, "notes": "<!-- dish-marco-authority:v1 decision=decision-1 action=takeover broker_admission=true -->"}
    kind, grant_id, generation = broker.decision_for_request(request, current=state, task=task, now=NOW)
    assert (kind, generation) == ("takeover", state.generation + 1)
    assert grant_id != state.grant_id
    assert broker.action_for_state_event(request, state) == state.action


def test_asana_hold_change_prevents_consequential_grant_even_with_repository_permission():
    request = broker.parse_request_comment(request_comment())
    pr = {
        "number": 95,
        "state": "open",
        "merged": False,
        "body": f"Owning task: {TASK}",
        "head": {"sha": HEAD, "ref": request.branch},
    }
    policy = {ROUTE: {"role": "implementation", "actions": ["fix"]}}
    with pytest.raises(broker.BrokerError, match="hold/not-ready"):
        broker.validate_live_request_preconditions(
            request,
            pr=pr,
            task={"gid": TASK, "completed": False, "notes": "Status: HOLD"},
            permission="admin",
            route_policy=policy,
        )


def test_repository_permission_never_creates_role_authority_without_allowed_route():
    request = broker.parse_request_comment(request_comment())
    pr = {
        "number": 95,
        "state": "open",
        "merged": False,
        "body": f"Owning task: {TASK}",
        "head": {"sha": HEAD, "ref": request.branch},
    }
    with pytest.raises(broker.BrokerError, match="standing role/action authority"):
        broker.validate_live_request_preconditions(
            request,
            pr=pr,
            task={"gid": TASK, "completed": False, "notes": "Status: ready"},
            permission="admin",
            route_policy={},
        )


def test_consumer_identity_is_deterministic_and_route_bound():
    grant = str(uuid.UUID(int=999))
    first = broker.consumer_id(REPO, 95, grant, 3, ROUTE)
    assert first == broker.consumer_id(REPO, 95, grant, 3, ROUTE)
    assert first != broker.consumer_id(REPO, 95, grant, 3, "other-route")


def lifecycle_for(state, *, gate=None, residual=None, external=None):
    return type("Lifecycle", (), {
        "state": state,
        "gate": gate,
        "residual_reason": residual,
        "external_dependency": external,
    })()


def test_fix_eligibility_is_bound_to_exact_current_block_review_id():
    request = broker.parse_request_comment(request_comment(action="fix", review=77))
    lifecycle = lifecycle_for(broker.LifecycleState.CHANGES_REQUESTED)
    reviews = [{"id": 77, "state": "COMMENTED", "commit_id": HEAD, "body": f"VERDICT: BLOCK\nReviewed head: {HEAD}", "submitted_at": NOW.isoformat()}]
    broker.validate_lifecycle_eligibility(
        request, lifecycle=lifecycle, reviews=reviews, live_main_sha=MAIN,
        integration_authority=False, current_grant=None,
    )
    stale = broker.parse_request_comment(request_comment(comment_id=44, action="fix", review=76))
    with pytest.raises(broker.BrokerError, match="current exact-head formal Review id"):
        broker.validate_lifecycle_eligibility(
            stale, lifecycle=lifecycle, reviews=reviews, live_main_sha=MAIN,
            integration_authority=False, current_grant=None,
        )


def test_pr_owned_ci_fix_does_not_accept_ambiguous_or_infrastructure_failure():
    request = broker.parse_request_comment(request_comment(action="fix", review=None))
    for ownership in ("AMBIGUOUS", "INFRASTRUCTURE", "PROVEN_CURRENT_MAIN"):
        lifecycle = lifecycle_for(
            broker.LifecycleState.CHANGES_REQUESTED,
            gate={"diagnosis": broker.pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value, "failure_ownership": ownership},
        )
        with pytest.raises(broker.BrokerError, match="PR-owned"):
            broker.validate_lifecycle_eligibility(
                request, lifecycle=lifecycle, reviews=[], live_main_sha=MAIN,
                integration_authority=False, current_grant=None,
            )


def test_v1a_broker_rejects_integration_merge_and_reconcile_requests_at_parse_boundary():
    for action in ("merge", "integration-reconcile"):
        with pytest.raises(broker.BrokerError, match="unsupported mutation action"):
            broker.parse_request_comment(
                request_comment(action=action, review=77, route="integration")
            )


def test_local_fix_route_rechecks_exact_local_only_review_classification():
    request = broker.parse_request_comment(request_comment(action="fix", review=77, route="local-implementation"))
    lifecycle = lifecycle_for(broker.LifecycleState.CHANGES_REQUESTED)
    route_policy = {
        "local-implementation": {"role": "implementation", "actions": ["fix"], "host": "local"}
    }
    unproven = [{
        "id": 77,
        "state": "COMMENTED",
        "commit_id": HEAD,
        "body": f"VERDICT: BLOCK\nTESTS TO RUN: NONE.\nReviewed head: {HEAD}",
        "submitted_at": NOW.isoformat(),
    }]
    with pytest.raises(broker.BrokerError, match="local Implementation fix route requires"):
        broker.validate_lifecycle_eligibility(
            request,
            lifecycle=lifecycle,
            reviews=unproven,
            live_main_sha=MAIN,
            integration_authority=False,
            current_grant=None,
            route_policy=route_policy,
        )

    proven = [{
        **unproven[0],
        "body": (
            "VERDICT: BLOCK\n"
            "LOCAL IMPLEMENTATION COMPLETION REQUIRED: IMPLEMENTATION / PUBLICATION — "
            "hosted publication cannot write governed path; fallbacks exhausted: connector update, Git data API\n"
            f"TESTS TO RUN: NONE.\nReviewed head: {HEAD}"
        ),
    }]
    broker.validate_lifecycle_eligibility(
        request,
        lifecycle=lifecycle,
        reviews=proven,
        live_main_sha=MAIN,
        integration_authority=False,
        current_grant=None,
        route_policy=route_policy,
    )
