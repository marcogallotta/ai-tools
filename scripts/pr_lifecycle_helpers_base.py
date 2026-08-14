"""Durable marker parsing and lifecycle helper predicates."""
from __future__ import annotations

from pr_lifecycle_support import *

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _marker_fields(body: str, marker: str) -> list[dict[str, str]]:
    pattern = re.compile(rf"<!--\s*{re.escape(marker)}\s+(?P<fields>.*?)\s*-->", re.IGNORECASE | re.DOTALL)
    values: list[dict[str, str]] = []
    for match in pattern.finditer(body or ""):
        fields: dict[str, str] = {}
        for token in match.group("fields").split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            fields[key.strip().lower()] = value.strip()
        values.append(fields)
    return values


def _pr_number(pr: Mapping[str, Any]) -> int:
    try:
        return int(pr["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleError("PR JSON is missing numeric number") from exc


def _pr_url(pr: Mapping[str, Any], repository: str) -> str:
    return str(pr.get("html_url") or pr.get("url") or f"https://github.com/{repository}/pull/{_pr_number(pr)}")


def _pr_title(pr: Mapping[str, Any]) -> str:
    return str(pr.get("title") or "")


def _pr_branch(pr: Mapping[str, Any]) -> str:
    head = pr.get("head")
    if isinstance(head, Mapping):
        return str(head.get("ref") or "")
    return str(pr.get("headRefName") or "")


def _pr_base(pr: Mapping[str, Any]) -> str:
    base = pr.get("base")
    if isinstance(base, Mapping):
        return str(base.get("ref") or "")
    return str(pr.get("baseRefName") or "")


def task_ids_from_pr(pr: Mapping[str, Any]) -> list[str]:
    text = "\n".join([str(pr.get("body") or ""), _pr_title(pr)])
    return sorted(set(TASK_GID_RE.findall(text)))


def _decode_marker_value(value: str | None, *, field: str) -> str:
    if value is None:
        raise LifecycleError(f"external dependency marker is missing {field}")
    decoded = urlparse.unquote(value).strip()
    if not decoded:
        raise LifecycleError(f"external dependency marker has empty {field}")
    return decoded


def parse_external_dependency(
    comments: Iterable[Mapping[str, Any]],
) -> ExternalDependency | None:
    """Return the newest durable external-dependency record, failing closed on malformed markers."""
    records: list[ExternalDependency] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        fields_list = _marker_fields(body, EXTERNAL_DEPENDENCY_MARKER)
        if not fields_list:
            continue
        timestamp = _parse_time(comment.get("updated_at") or comment.get("created_at"))
        if timestamp is None:
            raise LifecycleError("external dependency marker comment is missing a valid timestamp")
        raw_id = comment.get("id")
        try:
            comment_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise LifecycleError("external dependency marker comment is missing a numeric id") from exc
        for marker_index, fields in enumerate(fields_list):
            action = str(fields.get("action") or "").lower()
            if action not in {"blocked", "resolved"}:
                raise LifecycleError("external dependency marker action must be blocked or resolved")
            task_gid = str(fields.get("task") or "")
            if TASK_GID_RE.fullmatch(task_gid) is None:
                raise LifecycleError("external dependency marker has invalid task GID")
            owner_pr: int | None = None
            if fields.get("pr") not in {None, ""}:
                try:
                    owner_pr = int(str(fields["pr"]))
                except ValueError as exc:
                    raise LifecycleError("external dependency marker has invalid PR number") from exc
                if owner_pr <= 0:
                    raise LifecycleError("external dependency marker has invalid PR number")
            check = _decode_marker_value(fields.get("check"), field="check identity")
            main_sha = str(fields.get("main") or "").lower()
            if FULL_SHA_RE.fullmatch(main_sha) is None:
                raise LifecycleError("external dependency marker has invalid main SHA")
            evidence = _decode_marker_value(fields.get("evidence"), field="evidence reference")
            reason_value = fields.get("reason")
            reason = urlparse.unquote(reason_value).strip() if reason_value else None
            records.append(
                ExternalDependency(
                    action=action,
                    task_gid=task_gid,
                    owner_pr=owner_pr,
                    check=check,
                    main_sha=main_sha,
                    evidence=evidence,
                    reason=reason or None,
                    timestamp=timestamp,
                    comment_id=comment_id,
                    marker_index=marker_index,
                )
            )
    if not records:
        return None
    return max(records, key=lambda item: (item.timestamp, item.comment_id, item.marker_index))


def external_dependency_human_action(record: ExternalDependency) -> str:
    owner = f"PR #{record.owner_pr} / task {record.task_gid}" if record.owner_pr else f"task {record.task_gid}"
    return f"Waiting on {owner}: {record.check}. No action for Marco."


def pending_authoring_evidence(pr: Mapping[str, Any]) -> str | None:
    """Return explicitly unfinished task-scoped authoring evidence from a draft PR."""
    match = AUTHORING_EVIDENCE_PENDING_RE.search(str(pr.get("body") or ""))
    if match is None:
        return None
    value = match.group("value").strip()
    return value or None


def _continuation_key(head: str, evidence: str) -> str:
    return hashlib.sha256(f"{head}:{evidence}".encode("utf-8")).hexdigest()[:20]


def _continuation_handoff_present(
    comments: Iterable[Mapping[str, Any]], *, head: str, evidence: str
) -> bool:
    key = _continuation_key(head, evidence)
    for comment in comments:
        for fields in _marker_fields(
            str(comment.get("body") or ""), IMPLEMENTATION_CONTINUATION_MARKER
        ):
            if fields.get("head") == head and fields.get("key") == key:
                return True
    return False

def parse_leases(
    comments: Iterable[Mapping[str, Any]],
    *,
    current_head: str,
    reviews: Iterable[Mapping[str, Any]],
    pr_open: bool,
    now: datetime | None = None,
) -> list[Lease]:
    if not pr_open:
        return []
    now = now or _utcnow()
    exact_review = pr_gate.latest_exact_head_review(list(reviews), reviewed_head=current_head)
    events: dict[str, list[tuple[datetime, str, dict[str, str], int | None]]] = {}
    for comment in comments:
        body = str(comment.get("body") or "")
        created = _parse_time(comment.get("updated_at") or comment.get("created_at"))
        if created is None:
            continue
        comment_id = comment.get("id") if isinstance(comment.get("id"), int) else None
        for fields in _marker_fields(body, LEASE_MARKER):
            lease_id = fields.get("lease")
            if not lease_id:
                continue
            events.setdefault(lease_id, []).append((created, "lease", fields, comment_id))
        for fields in _marker_fields(body, LEASE_RELEASE_MARKER):
            lease_id = fields.get("lease")
            if not lease_id:
                continue
            events.setdefault(lease_id, []).append((created, "release", fields, comment_id))

    active: list[Lease] = []
    for lease_id, lease_events in events.items():
        lease_events.sort(key=lambda item: item[0])
        timestamp, event_type, fields, comment_id = lease_events[-1]
        if event_type != "lease":
            continue
        head = fields.get("head", "")
        phase = fields.get("phase", "")
        if head != current_head or not phase:
            continue
        if now - timestamp >= LEASE_STALE_AFTER:
            continue
        if phase == "review" and exact_review is not None:
            continue
        active.append(
            Lease(
                phase=phase,
                head=head,
                lease=lease_id,
                timestamp=timestamp,
                owner=fields.get("owner"),
                review_class=fields.get("class"),
                comment_id=comment_id,
            )
        )
    active.sort(key=lambda item: (item.phase, item.timestamp, item.lease))
    return active


def _completion_present(comments: Iterable[Mapping[str, Any]], *, kind: str, head: str) -> bool:
    matches: list[tuple[datetime, str]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        when = _parse_time(comment.get("updated_at") or comment.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
        for fields in _marker_fields(body, LOCAL_COMPLETION_MARKER):
            if fields.get("kind") != kind or fields.get("head") != head:
                continue
            matches.append((when, fields.get("result", "")))
    if not matches:
        return False
    result = max(matches, key=lambda item: item[0])[1]
    return result in {"pass", "passed", "complete", "completed", "success"}


def _handoff_key(kind: str, head: str, instruction: str) -> str:
    return hashlib.sha256(f"{kind}:{head}:{instruction}".encode("utf-8")).hexdigest()[:20]


def _handoff_present(comments: Iterable[Mapping[str, Any]], *, kind: str, head: str, instruction: str) -> bool:
    key = _handoff_key(kind, head, instruction)
    for comment in comments:
        for fields in _marker_fields(str(comment.get("body") or ""), LOCAL_HANDOFF_MARKER):
            if fields.get("kind") == kind and fields.get("head") == head and fields.get("key") == key:
                return True
    return False


def _notice_key(kind: str, head: str, action: str) -> str:
    return hashlib.sha256(f"{kind}:{head}:{action}".encode("utf-8")).hexdigest()[:20]


def _notice_present(comments: Iterable[Mapping[str, Any]], *, kind: str, head: str, action: str) -> bool:
    key = _notice_key(kind, head, action)
    for comment in comments:
        for fields in _marker_fields(str(comment.get("body") or ""), HUMAN_NOTICE_MARKER):
            if fields.get("kind") == kind and fields.get("head") == head and fields.get("key") == key:
                return True
    return False


def local_work_from_review(
    review: Mapping[str, Any] | None,
    comments: Iterable[Mapping[str, Any]],
    *,
    head: str,
) -> list[LocalWork]:
    if not review or review.get("verdict") != "MERGE":
        return []
    body = str(review.get("body") or "")
    work: list[LocalWork] = []
    implementation = LOCAL_IMPLEMENTATION_RE.search(body)
    if implementation:
        instruction = implementation.group("value").strip()
        work.append(
            LocalWork(
                kind="implementation",
                required=True,
                instruction=instruction,
                completed=_completion_present(comments, kind="implementation", head=head),
                handoff_present=_handoff_present(
                    comments, kind="implementation", head=head, instruction=instruction
                ),
            )
        )
    tests = TESTS_TO_RUN_RE.search(body)
    if tests:
        instruction = tests.group("value").strip()
        if instruction.rstrip(".").upper() != "NONE":
            work.append(
                LocalWork(
                    kind="certification",
                    required=True,
                    instruction=instruction,
                    completed=_completion_present(comments, kind="certification", head=head),
                    handoff_present=_handoff_present(
                        comments, kind="certification", head=head, instruction=instruction
                    ),
                )
            )
    return work


def review_class_for(
    pr: Mapping[str, Any],
    reviews: Iterable[Mapping[str, Any]],
    comments: Iterable[Mapping[str, Any]],
    *,
    current_head: str,
) -> str:
    sources = [str(pr.get("body") or "")]
    for comment in comments:
        for fields in _marker_fields(str(comment.get("body") or ""), REVIEW_ROUTE_MARKER):
            if fields.get("head") not in {None, "", current_head}:
                continue
            value = fields.get("class")
            if value:
                return value.lower()
    match = REVIEW_CLASS_RE.search(sources[0])
    if match:
        return _normalize_review_class(match.group("value"))

    prior: list[tuple[datetime, Mapping[str, Any]]] = []
    for review in reviews:
        body = str(review.get("body") or "")
        verdict = pr_gate.review_verdict(body)
        if verdict != "BLOCK":
            continue
        commit_id = str(review.get("commit_id") or "")
        if not commit_id or commit_id == current_head:
            continue
        when = _parse_time(review.get("submitted_at") or review.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
        prior.append((when, review))
    if prior:
        body = str(max(prior, key=lambda item: item[0])[1].get("body") or "")
        disposition = AFTER_FIX_RE.search(body)
        if disposition:
            value = disposition.group(1).upper()
            if value == "FOCUSED RECHECK":
                return "focused"
            if value == "MECHANICAL CHECK ONLY":
                return "mechanical"
            if value in {"DOMAIN DEEP RECHECK", "NEW SPECIALIST REVIEW"}:
                return "domain:unspecified"
            if value == "NORMAL MERGE REVIEW":
                return "substantive"
    return "substantive"


def _normalize_review_class(value: str) -> str:
    normalized = value.strip().lower().replace(" ", "-")
    aliases = {
        "ordinary": "substantive",
        "ordinary-substantive": "substantive",
        "normal-merge-review": "substantive",
        "focused-recheck": "focused",
        "mechanical-check-only": "mechanical",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {"light", "focused", "mechanical", "substantive"}:
        return normalized
    if normalized.startswith("domain:") and normalized.split(":", 1)[1]:
        return normalized
    # legacy marker: a domain label alone never justifies a second AI-reviewer
    # dependency, so specialist:<name> normalizes to the same in-workflow depth hint.
    if normalized.startswith("specialist:") and normalized.split(":", 1)[1]:
        return "domain:" + normalized.split(":", 1)[1]
    return "substantive"


def _mergeability_reason(pr: Mapping[str, Any]) -> str | None:
    mergeable = pr.get("mergeable")
    mergeable_state = str(pr.get("mergeable_state") or "").lower()
    if mergeable is False or mergeable_state in {"dirty"}:
        return "GitHub reports the exact reviewed head is not currently mergeable"
    if mergeable is None and mergeable_state in {"unknown", ""}:
        return "GitHub mergeability is not yet resolved"
    return None


def _integration_order_reason(review: Mapping[str, Any] | None, pr: Mapping[str, Any]) -> str | None:
    for text in (str(review.get("body") or "") if review else "", str(pr.get("body") or "")):
        match = INTEGRATION_BLOCK_RE.search(text)
        if match:
            return match.group("value").strip()
    return None


def _lease_json(lease: Lease, now: datetime) -> dict[str, Any]:
    return {
        "phase": lease.phase,
        "head": lease.head,
        "lease": lease.lease,
        "owner": lease.owner,
        "review_class": lease.review_class,
        "last_activity": lease.timestamp.isoformat(),
        "stale_at": (lease.timestamp + LEASE_STALE_AFTER).isoformat(),
        "age_seconds": max(0, int((now - lease.timestamp).total_seconds())),
    }


def _reviewed_head(review: Mapping[str, Any] | None) -> str | None:
    if not review:
        return None
    return str(review.get("commit_id") or "") or None


def implementation_continuation_lifecycle(
    *,
    base_kwargs: Mapping[str, Any],
    evidence: str,
    review_class: str | None,
    lease_payload: list[dict[str, Any]],
    implementation_active: bool,
) -> PRLifecycle:
    number = int(base_kwargs["number"])
    return PRLifecycle(
        **base_kwargs,
        state=LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED,
        state_label=STATE_LABELS[LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED],
        authoring_evidence=evidence,
        review_class=review_class,
        active_leases=lease_payload,
        residual_reason=(
            f"draft PR has unfinished task-scoped authoring evidence: {evidence}"
            + ("; implementation lease active" if implementation_active else "; no active implementation lease")
        ),
        human_action=f"PR #{number} still needs Implementation to finish {evidence}.",
    )
