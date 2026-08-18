"""Single-dispatch Review coordination for multi-PR implementation workstreams.

The workstream layer is a projection over durable PR metadata.  It adds no queue,
lock, ownership database, or Integration authority.  Constituent PRs remain the
publication/rework/rollback and exact-head Integration units.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from typing import Any, Iterable, Mapping
from urllib import parse as urlparse

from pr_lifecycle_support import (
    DISPATCH_OWNER,
    LEASE_MARKER,
    LEASE_RELEASE_MARKER,
    LEASE_STALE_AFTER,
    LifecycleError,
    LifecycleState,
    STATE_LABELS,
    TASK_GID_RE,
    WORKSPACE_RUNS_BETA,
    WorkspaceDispatchResult,
)
from pr_lifecycle_helpers import _marker_fields, _parse_time, _pr_number
from pr_lifecycle_engine_actions import (
    TERMINAL_RECOVERY_SLOT_SECONDS,
    _dispatch_fixer,
    _fixer_command,
    _route_result_marker,
)
from pr_lifecycle_host_routing import (
    CHATGPT_IMPLEMENTATION,
    implementation_host_for_review,
)

WORKSTREAM_MARKER = "dish-workstream:v1"
WORKSTREAM_REVIEW_MARKER = "dish-workstream-review:v1"
WORKSTREAM_DISPATCH_MARKER = "dish-workstream-review-dispatch:v1"
WORKSTREAM_FIX_DISPATCH_MARKER = "dish-workstream-fix-dispatch:v1"
VERDICT_RE = re.compile(r"(?im)^\s*VERDICT:\s*(MERGE|BLOCK)\s*$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


@dataclass(frozen=True)
class WorkstreamDeclaration:
    task: str
    slot: int
    total: int


@dataclass(frozen=True)
class WorkstreamMember:
    slot: int
    total: int
    pr_number: int
    pr_url: str
    branch: str
    base: str
    head: str
    publication_state: str
    task_ids: tuple[str, ...]
    owning_task: str | None

    def semantic_task_ids(self, workstream_task: str) -> tuple[str, ...]:
        values = tuple(task for task in self.task_ids if task != workstream_task)
        return values or self.task_ids


@dataclass(frozen=True)
class WorkstreamCandidate:
    workstream_task: str
    total: int
    members: tuple[WorkstreamMember, ...]
    complete: bool
    error: str | None
    shape_id: str
    candidate_id: str

    @property
    def anchor(self) -> WorkstreamMember:
        return self.members[0]

    def context(self, *, changed_prs: Iterable[int] = (), review_class: str = "substantive") -> dict[str, Any]:
        changed = sorted({int(value) for value in changed_prs})
        return {
            "schema": "dish-workstream-review-dispatch-v1",
            "workstream_task": self.workstream_task,
            "candidate_id": self.candidate_id,
            "shape_id": self.shape_id,
            "review_class": review_class,
            "changed_prs": changed,
            "members": [
                {
                    "slot": member.slot,
                    "total": member.total,
                    "pr_number": member.pr_number,
                    "pr_url": member.pr_url,
                    "branch": member.branch,
                    "base": member.base,
                    "head": member.head,
                    "publication_state": member.publication_state,
                    "task_ids": list(member.semantic_task_ids(self.workstream_task)),
                    "owning_task": member.owning_task,
                }
                for member in self.members
            ],
        }


@dataclass(frozen=True)
class WorkstreamReviewRecord:
    pr_number: int
    head: str
    workstream_task: str
    candidate_id: str
    shape_id: str
    verdict: str
    review: Mapping[str, Any]
    submitted_at: datetime


@dataclass(frozen=True)
class WorkstreamReviewState:
    status: str  # none | partial | merge | block
    records: tuple[WorkstreamReviewRecord, ...] = ()


def _owning_task_from_refs(task_ids: Iterable[str]) -> str | None:
    return getattr(task_ids, "owning_task_id", None)


def declaration_from_pr(pr: Mapping[str, Any]) -> WorkstreamDeclaration | None:
    markers = _marker_fields(str(pr.get("body") or ""), WORKSTREAM_MARKER)
    if not markers:
        return None
    declarations: list[WorkstreamDeclaration] = []
    for fields in markers:
        task = str(fields.get("task") or "")
        if TASK_GID_RE.fullmatch(task) is None:
            raise LifecycleError("workstream marker requires a 16-digit task identity")
        try:
            slot = int(str(fields.get("slot") or ""))
            total = int(str(fields.get("total") or ""))
        except ValueError as exc:
            raise LifecycleError("workstream marker slot/total must be integers") from exc
        if total < 2:
            raise LifecycleError("workstream marker is only valid for multi-PR workstreams (total >= 2)")
        if slot < 1 or slot > total:
            raise LifecycleError("workstream marker slot must be within 1..total")
        declarations.append(WorkstreamDeclaration(task=task, slot=slot, total=total))
    unique = set(declarations)
    if len(unique) != 1:
        raise LifecycleError("PR has conflicting workstream declarations")
    return declarations[0]


def _stable_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_candidate(workstream_task: str, members: Iterable[WorkstreamMember]) -> WorkstreamCandidate:
    ordered = tuple(sorted(members, key=lambda value: (value.slot, value.pr_number)))
    if not ordered:
        raise LifecycleError("workstream candidate requires at least one published PR")
    totals = {member.total for member in ordered}
    slots = [member.slot for member in ordered]
    total = max(totals)
    errors: list[str] = []
    if len(totals) != 1:
        errors.append(f"conflicting total declarations: {sorted(totals)}")
    if len(set(slots)) != len(slots):
        errors.append("duplicate workstream slots")
    expected = list(range(1, total + 1))
    if sorted(set(slots)) != expected:
        errors.append(f"published slots {sorted(set(slots))}; expected {expected}")
    complete = not errors and len(ordered) == total

    shape = {
        "schema": "dish-workstream-shape-v1",
        "workstream_task": workstream_task,
        "members": [
            {
                "slot": member.slot,
                "pr": member.pr_number,
                "branch": member.branch,
                "base": member.base,
                "semantic_tasks": list(member.semantic_task_ids(workstream_task)),
                "owning_task": member.owning_task,
            }
            for member in ordered
        ],
    }
    shape_id = _stable_hash(shape)
    identity = {
        "schema": "dish-workstream-candidate-v1",
        "shape_id": shape_id,
        "heads": [{"slot": member.slot, "pr": member.pr_number, "head": member.head} for member in ordered],
    }
    return WorkstreamCandidate(
        workstream_task=workstream_task,
        total=total,
        members=ordered,
        complete=complete,
        error="; ".join(errors) if errors else None,
        shape_id=shape_id,
        candidate_id=_stable_hash(identity),
    )


def _review_verdict(review: Mapping[str, Any]) -> str | None:
    match = VERDICT_RE.search(str(review.get("body") or ""))
    return match.group(1).upper() if match else None


def _review_head(review: Mapping[str, Any]) -> str:
    return str(review.get("commit_id") or review.get("commitId") or "")


def _review_time(review: Mapping[str, Any]) -> datetime:
    return _parse_time(review.get("submitted_at") or review.get("submittedAt")) or datetime.min.replace(tzinfo=timezone.utc)


def review_records_for_pr(
    reviews: Iterable[Mapping[str, Any]], *, pr_number: int, workstream_task: str
) -> list[WorkstreamReviewRecord]:
    records: list[WorkstreamReviewRecord] = []
    for review in reviews:
        if str(review.get("state") or "").upper() not in {"COMMENTED", "COMMENT"}:
            continue
        verdict = _review_verdict(review)
        if verdict is None:
            continue
        head = _review_head(review)
        if FULL_SHA_RE.fullmatch(head) is None:
            continue
        for fields in _marker_fields(str(review.get("body") or ""), WORKSTREAM_REVIEW_MARKER):
            if str(fields.get("workstream") or "") != workstream_task:
                continue
            candidate = str(fields.get("candidate") or "")
            shape = str(fields.get("shape") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", candidate) or not re.fullmatch(r"[0-9a-f]{64}", shape):
                continue
            records.append(
                WorkstreamReviewRecord(
                    pr_number=pr_number,
                    head=head,
                    workstream_task=workstream_task,
                    candidate_id=candidate,
                    shape_id=shape,
                    verdict=verdict,
                    review=review,
                    submitted_at=_review_time(review),
                )
            )
    records.sort(key=lambda item: (item.submitted_at, int(item.review.get("id") or 0)))
    return records


def current_review_state(candidate: WorkstreamCandidate, github: Any) -> WorkstreamReviewState:
    """Resolve one workstream verdict while preserving per-PR exact-head evidence.

    Open members must carry the current candidate marker. Already-merged predecessors
    may retain their earlier exact-head MERGE record for the same publication shape;
    they are immutable Integration history, not fresh Review targets.
    """
    matched: list[WorkstreamReviewRecord] = []
    open_records: list[WorkstreamReviewRecord] = []
    evidence_seen = False
    missing = False
    for member in candidate.members:
        records = review_records_for_pr(
            github.get_reviews(member.pr_number),
            pr_number=member.pr_number,
            workstream_task=candidate.workstream_task,
        )
        exact_shape = [
            record
            for record in records
            if record.head == member.head and record.shape_id == candidate.shape_id
        ]
        current = [record for record in exact_shape if record.candidate_id == candidate.candidate_id]
        evidence_seen = evidence_seen or bool(exact_shape)
        if member.publication_state == "merged":
            inherited = current[-1] if current else next(
                (record for record in reversed(exact_shape) if record.verdict == "MERGE"),
                None,
            )
            if inherited is None or inherited.verdict != "MERGE":
                missing = True
                continue
            matched.append(inherited)
            continue
        if member.publication_state != "open":
            missing = True
            continue
        if not current:
            missing = True
            continue
        record = current[-1]
        matched.append(record)
        open_records.append(record)

    if not matched and not evidence_seen:
        return WorkstreamReviewState("none")
    if missing:
        return WorkstreamReviewState("partial", tuple(matched))
    if not open_records:
        return WorkstreamReviewState("merge", tuple(matched))
    verdicts = {record.verdict for record in open_records}
    if verdicts == {"MERGE"}:
        return WorkstreamReviewState("merge", tuple(matched))
    if verdicts == {"BLOCK"}:
        return WorkstreamReviewState("block", tuple(matched))
    return WorkstreamReviewState("partial", tuple(matched))


def previous_complete_candidate(candidate: WorkstreamCandidate, github: Any) -> tuple[str, str, dict[int, str], str] | None:
    """Return the newest completed Review generation that covers this publication shape.

    Narrow re-reviews mechanically refresh every still-open member. Merged predecessors
    inherit their earlier exact-head MERGE artifact, allowing later generations to remain
    reconstructable without reopening or re-reviewing already-landed PRs.
    """
    per_pr: dict[int, list[WorkstreamReviewRecord]] = {}
    all_keys: set[tuple[str, str]] = set()
    for member in candidate.members:
        records = review_records_for_pr(
            github.get_reviews(member.pr_number),
            pr_number=member.pr_number,
            workstream_task=candidate.workstream_task,
        )
        per_pr[member.pr_number] = records
        all_keys.update((record.candidate_id, record.shape_id) for record in records)
    if not all_keys:
        return None

    generations: list[tuple[datetime, str, str, dict[int, str], str]] = []
    for candidate_id, shape_id in all_keys:
        heads: dict[int, str] = {}
        open_verdicts: set[str] = set()
        newest = datetime.min.replace(tzinfo=timezone.utc)
        complete = True
        for member in candidate.members:
            records = per_pr[member.pr_number]
            matches = [
                record
                for record in records
                if record.candidate_id == candidate_id and record.shape_id == shape_id
            ]
            if matches:
                record = matches[-1]
                heads[member.pr_number] = record.head
                newest = max(newest, record.submitted_at)
                if member.publication_state == "open":
                    open_verdicts.add(record.verdict)
                elif member.publication_state == "merged" and record.verdict != "MERGE":
                    complete = False
                    break
                elif member.publication_state == "closed":
                    complete = False
                    break
                continue
            if member.publication_state != "merged":
                complete = False
                break
            inherited = [
                record
                for record in records
                if record.head == member.head and record.shape_id == shape_id and record.verdict == "MERGE"
            ]
            if not inherited:
                complete = False
                break
            record = inherited[-1]
            heads[member.pr_number] = record.head
            newest = max(newest, record.submitted_at)
        if not complete or len(open_verdicts) > 1:
            continue
        verdict = next(iter(open_verdicts)) if open_verdicts else "MERGE"
        generations.append((newest, candidate_id, shape_id, heads, verdict))
    if not generations:
        return None
    _, candidate_id, shape_id, heads, verdict = max(generations, key=lambda item: item[0])
    return candidate_id, shape_id, heads, verdict


def recheck_scope(candidate: WorkstreamCandidate, github: Any) -> tuple[str, list[int], str | None]:
    previous = previous_complete_candidate(candidate, github)
    if previous is None:
        return "substantive", [], None
    previous_id, previous_shape, previous_heads, _ = previous
    changed = [member.pr_number for member in candidate.members if previous_heads.get(member.pr_number) != member.head]
    if not changed:
        return "substantive", [], previous_id
    if previous_shape != candidate.shape_id:
        return "substantive", changed, previous_id
    return "focused", changed, previous_id



def _encoded_candidate_context(
    candidate: WorkstreamCandidate, *, review_class: str = "substantive", changed_prs: Iterable[int] = ()
) -> str:
    payload = candidate.context(changed_prs=changed_prs, review_class=review_class)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return urlparse.quote(raw, safe="")


def _decoded_candidate_context(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        payload = json.loads(urlparse.unquote(value))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != "dish-workstream-review-dispatch-v1":
        return None
    if not isinstance(payload.get("members"), list):
        return None
    return payload

def _active_marker(
    github: Any,
    candidate: WorkstreamCandidate,
    *,
    marker: str,
    phase: str,
    now: datetime,
) -> bool:
    for member in candidate.members:
        for comment in github.get_comments(member.pr_number):
            timestamp = _parse_time(comment.get("updated_at") or comment.get("created_at"))
            if timestamp is None or now - timestamp >= LEASE_STALE_AFTER:
                continue
            body = str(comment.get("body") or "")
            for fields in _marker_fields(body, marker):
                if (
                    fields.get("workstream") == candidate.workstream_task
                    and fields.get("candidate") == candidate.candidate_id
                    and fields.get("phase") == phase
                ):
                    return True
    return False


def _workspace_idempotency_key(repository: str, candidate: WorkstreamCandidate, review_class: str) -> str:
    identity = (
        f"dish-workstream-review:v1:{repository}:{candidate.workstream_task}:"
        f"{candidate.candidate_id}:{review_class}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _dispatch_workspace_review(
    workspace: Any,
    *,
    repository: str,
    candidate: WorkstreamCandidate,
    review_class: str,
    changed_prs: list[int],
    previous_candidate_id: str | None,
) -> WorkspaceDispatchResult:
    if not getattr(workspace, "access_token", None):
        raise LifecycleError("Workspace Agent access token is unavailable")
    trigger_id = workspace.trigger_id_for(review_class)
    if not trigger_id:
        raise LifecycleError("published ChatGPT Review Workspace Agent trigger is unavailable")
    key = _workspace_idempotency_key(repository, candidate, review_class)
    context = candidate.context(changed_prs=changed_prs, review_class=review_class)
    context["previous_candidate_id"] = previous_candidate_id
    context_json = json.dumps(context, sort_keys=True)
    if changed_prs:
        scope_instruction = (
            f"This is a narrow re-review. Changed PRs: {changed_prs}. Re-read the prior workstream Review trail, "
            "recheck those exact heads plus affected cross-PR interactions, and broaden only if a wider assumption "
            "actually changed; state the reason if you broaden."
        )
    else:
        scope_instruction = (
            "This is the broad initial workstream Review. Review every member task/PR and cross-PR interaction "
            "as one candidate."
        )
    prompt = (
        "Review exactly one Dish multi-PR implementation workstream as the standing Review role. "
        f"Repository: {repository}. Workstream context: {context_json}. {scope_instruction} "
        "This is ONE human/agent Review dispatch, not one dispatch per PR. Member tasks retain their own semantic "
        "authority; PRs retain publication/rework/rollback identity; Integration remains per-PR and dependency ordered. "
        "Before verdict, re-read every listed PR and exact head and inspect ordering plus cross-PR interactions. "
        "Return one consolidated blocker set to the workstream owner if blocked. Mechanically submit one formal GitHub "
        "COMMENT review on EACH currently-open listed PR, anchored to that PR's exact listed head, using the SAME broad "
        "verdict on those open members. A listed merged predecessor is immutable prior Integration history: inspect its "
        "reviewed interaction context but do not try to submit a new review to the merged PR. Every new formal review body "
        "must contain exactly one marker "
        f"`<!-- {WORKSTREAM_REVIEW_MARKER} workstream={candidate.workstream_task} "
        f"candidate={candidate.candidate_id} shape={candidate.shape_id} -->` and `VERDICT: MERGE` or `VERDICT: BLOCK`. "
        "Those per-PR records are durable exact-head evidence from this single Review operation; they are not separate "
        "Review dispatches. Preserve normal PRE-INTEGRATION TESTS TO RUN / POST-MERGE GATES metadata on each MERGE "
        "record as applicable. Do not implement fixes or perform Integration. Read and follow the current "
        "dish/docs/agents/review.md contract."
    )
    headers = {
        "Authorization": f"Bearer {workspace.access_token}",
        "OpenAI-Beta": WORKSPACE_RUNS_BETA,
        "Idempotency-Key": key,
        "Content-Type": "application/json",
    }
    status, _, value = workspace.http.request(
        "POST",
        f"{workspace.api_root}/workspace_agents/{trigger_id}/trigger",
        headers=headers,
        body={
            "conversation_key": (
                f"dish-workstream-{repository.replace('/', '-')}-{candidate.workstream_task}-"
                f"{candidate.candidate_id[:24]}"
            ),
            "input": prompt,
        },
    )
    if status != 202:
        raise LifecycleError(f"Workspace Agent trigger was not accepted: HTTP {status}")
    payload = value if isinstance(value, dict) else {}
    return WorkspaceDispatchResult(
        idempotency_key=key,
        accepted=True,
        status_code=status,
        conversation_url=payload.get("conversation_url"),
        run_id=payload.get("agent_trigger_run_id"),
    )


class WorkstreamLifecycleMixin:
    """Overlay single-dispatch workstream Review onto the existing stateless lifecycle."""

    @staticmethod
    def _member_from_lifecycle(value: Any, declaration: WorkstreamDeclaration) -> WorkstreamMember:
        return WorkstreamMember(
            slot=declaration.slot,
            total=declaration.total,
            pr_number=value.number,
            pr_url=value.url,
            branch=value.branch,
            base=value.base,
            head=value.head,
            publication_state=(
                "merged" if value.state == LifecycleState.MERGED
                else "closed" if value.state == LifecycleState.CLOSED
                else "open"
            ),
            task_ids=tuple(value.task_ids),
            owning_task=_owning_task_from_refs(value.task_ids),
        )

    def _recover_candidate_from_dispatch(
        self, candidate: WorkstreamCandidate
    ) -> WorkstreamCandidate:
        """Recover already-landed members from durable workstream dispatch context.

        Ordinary status scans only open PRs. Once Integration lands an earlier member,
        surviving PR comments still carry the exact reviewed publication set, allowing
        restart recovery without an all-history closed-PR scan or a second database.
        """
        contexts: list[tuple[datetime, int, dict[str, Any]]] = []
        for member in candidate.members:
            for comment in self.github.get_comments(member.pr_number):
                timestamp = _parse_time(comment.get("updated_at") or comment.get("created_at"))
                if timestamp is None:
                    continue
                try:
                    comment_id = int(comment.get("id") or 0)
                except (TypeError, ValueError):
                    comment_id = 0
                for fields in _marker_fields(str(comment.get("body") or ""), WORKSTREAM_DISPATCH_MARKER):
                    if fields.get("workstream") != candidate.workstream_task:
                        continue
                    payload = _decoded_candidate_context(fields.get("context"))
                    if payload is None or payload.get("workstream_task") != candidate.workstream_task:
                        continue
                    contexts.append((timestamp, comment_id, payload))
        if not contexts:
            return candidate
        _, _, context = max(contexts, key=lambda item: (item[0], item[1]))
        described = context.get("members")
        if not isinstance(described, list):
            return candidate

        recovered: dict[int, WorkstreamMember] = {member.pr_number: member for member in candidate.members}
        for item in described:
            if not isinstance(item, dict):
                return candidate
            try:
                pr_number = int(item.get("pr_number"))
            except (TypeError, ValueError):
                return candidate
            if pr_number in recovered:
                continue
            try:
                raw = self.github.get_pr(pr_number)
            except (LifecycleError, AssertionError):
                return candidate
            declaration = declaration_from_pr(raw)
            if declaration is None or declaration.task != candidate.workstream_task:
                return candidate
            try:
                expected_slot = int(item.get("slot"))
                expected_total = int(item.get("total"))
            except (TypeError, ValueError):
                return candidate
            if declaration.slot != expected_slot or declaration.total != expected_total:
                return candidate
            lifecycle = self.inspect(raw)
            recovered[pr_number] = self._member_from_lifecycle(lifecycle, declaration)
        rebuilt = build_candidate(candidate.workstream_task, recovered.values())
        # Recovery context may locate missing PRs, but live PR metadata remains the source
        # of truth for every head/base/task field used in the rebuilt candidate.
        return rebuilt

    def _workstream_candidates(self, values: Iterable[Any]) -> dict[str, WorkstreamCandidate]:
        grouped: dict[str, list[WorkstreamMember]] = {}
        for value in values:
            raw = self.github.get_pr(value.number)
            declaration = declaration_from_pr(raw)
            if declaration is None:
                continue
            grouped.setdefault(declaration.task, []).append(
                self._member_from_lifecycle(value, declaration)
            )
        candidates: dict[str, WorkstreamCandidate] = {}
        for task, members in grouped.items():
            candidate = build_candidate(task, members)
            if not candidate.complete:
                candidate = self._recover_candidate_from_dispatch(candidate)
            candidates[task] = candidate
        return candidates

    def _candidate_member_values(self, candidate: WorkstreamCandidate, values: Iterable[Any]) -> list[Any]:
        by_number = {value.number: value for value in values}
        return [by_number[member.pr_number] for member in candidate.members if member.pr_number in by_number]

    def _overlay_workstream_status(self, values: list[Any]) -> list[Any]:
        candidates = self._workstream_candidates(values)
        for candidate in candidates.values():
            members = self._candidate_member_values(candidate, values)
            if not candidate.complete:
                for value in members:
                    value.state = LifecycleState.REVIEW_READY
                    value.state_label = STATE_LABELS[LifecycleState.REVIEW_READY]
                    value.review_verdict = None
                    value.residual_reason = (
                        f"workstream {candidate.workstream_task} is not review-complete: {candidate.error or 'missing member PR'}"
                    )
                    value.human_action = None
                continue
            state = current_review_state(candidate, self.github)
            review_class, changed_prs, _ = recheck_scope(candidate, self.github)
            if state.status == "merge":
                continue
            if state.status == "block":
                for value in members:
                    value.state = LifecycleState.CHANGES_REQUESTED
                    value.state_label = STATE_LABELS[LifecycleState.CHANGES_REQUESTED]
                    value.review_verdict = "BLOCK"
                    value.residual_reason = (
                        f"workstream candidate {candidate.candidate_id[:12]} is BLOCKED; "
                        "one consolidated Implementation/fix dispatch owns the blocker set"
                    )
                    value.human_action = None
                continue
            active_review = _active_marker(
                self.github,
                candidate,
                marker=WORKSTREAM_DISPATCH_MARKER,
                phase="review",
                now=self.now(),
            )
            for value in members:
                if active_review or state.status == "partial":
                    value.state = LifecycleState.REVIEW_IN_PROGRESS
                    value.state_label = STATE_LABELS[LifecycleState.REVIEW_IN_PROGRESS]
                    value.residual_reason = (
                        f"single workstream Review is in progress for candidate {candidate.candidate_id[:12]}"
                    )
                else:
                    value.state = LifecycleState.REVIEW_READY
                    value.state_label = STATE_LABELS[LifecycleState.REVIEW_READY]
                    value.review_class = review_class
                    scope = f"changed PRs {changed_prs}" if changed_prs else "all members"
                    value.residual_reason = (
                        f"workstream {candidate.workstream_task} is reviewable as one candidate; scope: {scope}"
                    )
                value.review_verdict = None
                value.human_action = None
        return values

    def status(self, *, include_closed: bool = False) -> list[Any]:
        values = super().status(include_closed=include_closed)
        return self._overlay_workstream_status(values)

    def _write_shared_lease(
        self,
        candidate: WorkstreamCandidate,
        *,
        phase: str,
        review_class: str | None = None,
        idempotency_key: str | None = None,
        changed_prs: Iterable[int] = (),
    ) -> str:
        lease_id = (
            str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))
            if idempotency_key
            else str(uuid.uuid4())
        )
        class_field = f" class={review_class}" if review_class else ""
        marker_kind = WORKSTREAM_DISPATCH_MARKER if phase == "review" else WORKSTREAM_FIX_DISPATCH_MARKER
        context_token = _encoded_candidate_context(
            candidate,
            review_class=review_class or "substantive",
            changed_prs=changed_prs,
        )
        for member in candidate.members:
            if member.publication_state != "open":
                continue
            lease = (
                f"<!-- {LEASE_MARKER} phase={phase} head={member.head} lease={lease_id} "
                f"owner={DISPATCH_OWNER}{class_field} workstream={candidate.workstream_task} "
                f"candidate={candidate.candidate_id} -->"
            )
            workstream = (
                f"<!-- {marker_kind} phase={phase} workstream={candidate.workstream_task} "
                f"candidate={candidate.candidate_id} shape={candidate.shape_id} lease={lease_id} "
                f"context={context_token} -->"
            )
            self.github.add_comment(
                member.pr_number,
                f"{lease}\n{workstream}\n{phase.upper()} CLAIMED — single workstream operation for exact head "
                f"`{member.head}`. Lease is advisory and stale after 60m without structured activity.\n\n"
                "— Dish PR lifecycle dispatcher",
            )
        return lease_id

    def _release_shared_lease(self, candidate: WorkstreamCandidate, lease_id: str, *, reason: str) -> None:
        marker = f"<!-- {LEASE_RELEASE_MARKER} lease={lease_id} -->"
        for member in candidate.members:
            if member.publication_state != "open":
                continue
            self.github.add_comment(
                member.pr_number,
                f"{marker}\nWorkstream lease released: {reason}\n\n— Dish PR lifecycle dispatcher",
            )

    def _dispatch_workstream_review(
        self,
        candidate: WorkstreamCandidate,
        *,
        workspace: Any,
        notify: Any,
    ) -> None:
        review_class, changed_prs, previous_candidate_id = recheck_scope(candidate, self.github)
        if _active_marker(
            self.github,
            candidate,
            marker=WORKSTREAM_DISPATCH_MARKER,
            phase="review",
            now=self.now(),
        ):
            return
        if workspace is None:
            anchor = self.inspect(self.github.get_pr(candidate.anchor.pr_number))
            anchor.residual_reason = "ChatGPT Review dispatch adapter is not configured for the workstream"
            anchor.human_action = "configure the published Review Workspace Agent trigger"
            self._notify_once(
                anchor,
                kind="workstream-review-dispatch-config",
                action=anchor.human_action,
                message=(
                    f"Workstream {candidate.workstream_task} — Review dispatch unavailable. "
                    "Action: configure the published Review Workspace Agent trigger."
                ),
                notify=notify,
            )
            return
        result = _dispatch_workspace_review(
            workspace,
            repository=self.github.repository,
            candidate=candidate,
            review_class=review_class,
            changed_prs=changed_prs,
            previous_candidate_id=previous_candidate_id,
        )
        self._write_shared_lease(
            candidate,
            phase="review",
            review_class=review_class,
            idempotency_key=result.idempotency_key,
            changed_prs=changed_prs,
        )

    def _dispatch_workstream_fix(
        self,
        candidate: WorkstreamCandidate,
        state: WorkstreamReviewState,
        *,
        implementation_fixer: Any,
    ) -> None:
        if state.status != "block":
            return
        open_members = [member for member in candidate.members if member.publication_state == "open"]
        if not open_members:
            return
        review_by_pr = {record.pr_number: record for record in state.records}
        if any(member.pr_number not in review_by_pr for member in open_members):
            return
        hosts = {
            implementation_host_for_review(review_by_pr[member.pr_number].review)
            for member in open_members
        }
        if len(hosts) != 1:
            raise LifecycleError(
                f"workstream {candidate.workstream_task} BLOCK has mixed Implementation host routing: {sorted(hosts)}"
            )
        host = next(iter(hosts))
        selected_command = None if implementation_fixer is None else _fixer_command(implementation_fixer, host)
        if selected_command is None:
            return

        grants: dict[int, Any] = {}
        broker_route: str | None = None
        lease_id: str | None = None
        if getattr(self, "mutation_broker_enabled", False):
            route_key = "fix-local" if host != CHATGPT_IMPLEMENTATION else "fix-chatgpt"
            broker_route = self._broker_route(route_key)
            if broker_route is None and host == CHATGPT_IMPLEMENTATION:
                broker_route = self._broker_route("fix")
            if not broker_route:
                raise LifecycleError(
                    f"mutation broker is active but no {host} Implementation/fix route is configured"
                )
            waiting = False
            for member in open_members:
                lifecycle = self.inspect(self.github.get_pr(member.pr_number))
                grant = self._broker_grant_for(lifecycle, action="fix", route=broker_route)
                if grant is None:
                    review = review_by_pr[member.pr_number].review
                    try:
                        review_id = int(review.get("id"))
                    except (TypeError, ValueError) as exc:
                        raise LifecycleError("workstream formal BLOCK review lacks numeric id") from exc
                    self._submit_broker_request(
                        lifecycle,
                        action="fix",
                        route=broker_route,
                        review_id=review_id,
                    )
                    waiting = True
                else:
                    grants[member.pr_number] = grant
            if waiting:
                return
        else:
            if _active_marker(
                self.github,
                candidate,
                marker=WORKSTREAM_FIX_DISPATCH_MARKER,
                phase="fix",
                now=self.now(),
            ):
                return
            lease_id = self._write_shared_lease(candidate, phase="fix")

        # Re-read every exact head and candidate Review before the single owner dispatch.
        fresh_values = {member.pr_number: self.inspect(self.github.get_pr(member.pr_number)) for member in candidate.members}
        fresh_members = tuple(
            WorkstreamMember(
                slot=member.slot,
                total=member.total,
                pr_number=member.pr_number,
                pr_url=fresh_values[member.pr_number].url,
                branch=fresh_values[member.pr_number].branch,
                base=fresh_values[member.pr_number].base,
                head=fresh_values[member.pr_number].head,
                publication_state=(
                    "merged" if fresh_values[member.pr_number].state == LifecycleState.MERGED
                    else "closed" if fresh_values[member.pr_number].state == LifecycleState.CLOSED
                    else "open"
                ),
                task_ids=tuple(fresh_values[member.pr_number].task_ids),
                owning_task=_owning_task_from_refs(fresh_values[member.pr_number].task_ids),
            )
            for member in candidate.members
        )
        fresh_candidate = build_candidate(candidate.workstream_task, fresh_members)
        if fresh_candidate.candidate_id != candidate.candidate_id:
            if lease_id is not None:
                self._release_shared_lease(candidate, lease_id, reason="candidate moved before fix dispatch")
            return
        fresh_state = current_review_state(fresh_candidate, self.github)
        if fresh_state.status != "block":
            if lease_id is not None:
                self._release_shared_lease(candidate, lease_id, reason="BLOCK evidence changed before fix dispatch")
            return

        context = {
            "schema": "dish-workstream-fix-dispatch-v1",
            "repository": self.github.repository,
            "workstream_task": candidate.workstream_task,
            "candidate_id": candidate.candidate_id,
            "shape_id": candidate.shape_id,
            "members": [
                {
                    "slot": member.slot,
                    "pr_number": member.pr_number,
                    "pr_url": member.pr_url,
                    "branch": member.branch,
                    "blocked_head": member.head,
                    "task_ids": list(member.semantic_task_ids(candidate.workstream_task)),
                    "owning_task": member.owning_task,
                    "publication_state": member.publication_state,
                    "mutable": member.publication_state == "open",
                    "formal_block_review": (
                        review_by_pr[member.pr_number].review
                        if member.pr_number in review_by_pr and member.publication_state == "open"
                        else None
                    ),
                    "mutation_grant": (
                        None
                        if member.pr_number not in grants
                        else {
                            "grant_id": grants[member.pr_number].grant_id,
                            "generation": grants[member.pr_number].generation,
                            "consumer_id": grants[member.pr_number].consumer_id,
                            "route": grants[member.pr_number].route,
                            "starting_head": grants[member.pr_number].starting_head,
                            "event_comment_id": grants[member.pr_number].event_comment_id,
                        }
                    ),
                }
                for member in candidate.members
            ],
            "instruction": (
                "Follow the current repository Implementation contract as the original bounded workstream owner. "
                "Treat the formal workstream BLOCK records as ONE consolidated blocker set. Update only affected "
                "existing PR branches. Preserve each member task's semantic authority and each PR's publication unit. "
                "Return changed exact heads; re-review will be limited to changed heads and affected interactions "
                "unless the publication shape or wider assumptions changed."
            ),
        }
        try:
            _dispatch_fixer(implementation_fixer, context, host=host)
        except LifecycleError:
            if lease_id is not None:
                self._release_shared_lease(candidate, lease_id, reason="workstream implementation/fix dispatcher failed")
            raise

        changed = False
        after: dict[int, Any] = {}
        for member in candidate.members:
            lifecycle = self.inspect(self.github.get_pr(member.pr_number))
            after[member.pr_number] = lifecycle
            changed = changed or lifecycle.head != member.head
        if changed and lease_id is not None:
            self._release_shared_lease(candidate, lease_id, reason="workstream fixer returned changed candidate head(s)")
        if changed and grants and broker_route is not None:
            for member in open_members:
                grant = grants[member.pr_number]
                lifecycle = after[member.pr_number]
                if lifecycle.head != member.head:
                    marker = _route_result_marker(
                        starting_head=member.head,
                        new_head=lifecycle.head,
                        host=host,
                        route=broker_route,
                        grant=grant,
                    )
                    self.github.add_comment(
                        member.pr_number,
                        f"{marker}\nWorkstream Implementation consumer returned exact new head `{lifecycle.head}`.\n\n"
                        "— Dish PR lifecycle dispatcher",
                    )
                self._submit_broker_request(
                    lifecycle,
                    action="complete",
                    route=grant.route,
                    grant_id=grant.grant_id,
                    generation=grant.generation,
                )

    def dispatch(
        self,
        *,
        include_closed: bool = False,
        workspace: Any = None,
        local_reviewer: Any = None,
        implementation_fixer: Any = None,
        terminal_cleaner: Any = None,
        notify: Any = None,
    ) -> list[Any]:
        # Keep the existing bounded terminal-recovery scan; only review/fix routing is grouped.
        notify = notify or (lambda _: None)
        raw_values = super().status()
        candidate_reader = getattr(self.github, "closed_recovery_candidate", None)
        if callable(candidate_reader):
            slot = int(self.now().timestamp() // TERMINAL_RECOVERY_SLOT_SECONDS)
            candidate_pr = candidate_reader(recovery_slot=slot)
            seen = {value.number for value in raw_values}
            if candidate_pr is not None and _pr_number(candidate_pr) not in seen:
                raw_values.append(self.inspect(candidate_pr))
        elif include_closed:
            seen = {value.number for value in raw_values}
            raw_values.extend(
                value
                for value in super().status(include_closed=True)
                if value.number not in seen
            )

        candidates = self._workstream_candidates(raw_values)
        grouped_numbers = {
            member.pr_number
            for candidate in candidates.values()
            for member in candidate.members
        }

        for candidate in candidates.values():
            if not candidate.complete:
                continue
            member_values = self._candidate_member_values(candidate, raw_values)
            # Merged predecessors remain immutable Review context while later members land.
            # Closed-unmerged members are a real publication blocker. Still-open members
            # must be out of authoring before the one workstream Review can run.
            if any(member.publication_state == "closed" for member in candidate.members):
                continue
            if any(
                value.draft
                or value.state
                in {
                    LifecycleState.AUTHORING,
                    LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED,
                }
                for value in member_values
            ):
                continue
            review_state = current_review_state(candidate, self.github)
            if review_state.status == "merge":
                # Broad Review has passed this exact candidate. Existing per-PR lifecycle now owns
                # local gates, CI and dependency-ordered exact-head Integration independently.
                continue
            if review_state.status == "block":
                self._dispatch_workstream_fix(
                    candidate,
                    review_state,
                    implementation_fixer=implementation_fixer,
                )
                continue
            self._dispatch_workstream_review(
                candidate,
                workspace=workspace,
                notify=notify,
            )

        results: list[Any] = []
        # For a candidate with current broad MERGE evidence, constituent PRs flow through
        # unchanged per-PR Integration logic. All other grouped members are held at the
        # workstream Review/fix layer and are never independently Review-dispatched.
        passed_group_numbers: set[int] = set()
        for candidate in candidates.values():
            if candidate.complete and current_review_state(candidate, self.github).status == "merge":
                passed_group_numbers.update(member.pr_number for member in candidate.members)

        for value in raw_values:
            if value.number not in grouped_numbers or value.number in passed_group_numbers:
                results.append(
                    self.dispatch_one(
                        value,
                        workspace=workspace,
                        local_reviewer=local_reviewer,
                        implementation_fixer=implementation_fixer,
                        terminal_cleaner=terminal_cleaner,
                        notify=notify,
                    )
                )
            else:
                results.append(self.inspect(self.github.get_pr(value.number)))
        return self._overlay_workstream_status(results)
