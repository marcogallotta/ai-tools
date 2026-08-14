# PR lifecycle dispatcher runbook

This runbook operates the repository-owned Dish PR lifecycle dispatcher. It is an orchestration surface, not semantic Review or Implementation authority and not a replacement for Integration gates.

## Authority and recovery model

`scripts/pr_lifecycle.py` derives queue truth from durable GitHub PR state and the linked Asana task identity. It has no authoritative local database. Restart recovery is a fresh GitHub/Asana read.

The exact PR source head SHA is the lifecycle identity for Review, leases, local-work handoff, certification, and Integration. A head change immediately invalidates exact-head reviews, leases, local-work completion markers, and certification markers from the older head.

The dispatcher reuses `scripts/pr_gate.py` for ordinary Review discoverability and exact-head ordinary-CI / Integration gate semantics. Do not create a second exact-head gate engine in this tool.

## Commands

From the repository root:

```sh
python scripts/pr_lifecycle.py status --format json
python scripts/pr_lifecycle.py status --format table
python scripts/pr_lifecycle.py dispatch --format table
python scripts/pr_lifecycle.py watch --interval 180
python scripts/pr_lifecycle.py watch --dispatch --interval 180
```

`status` is read-only. `dispatch` performs one idempotent routing pass. `watch --dispatch` repeats the same pass; repeated polls must be safe.

GitHub authentication comes from `GITHUB_TOKEN` or `GH_TOKEN`. `ASANA_ACCESS_TOKEN` is optional for linked-task metadata; missing Asana access is surfaced rather than replaced by local state. Writable Implementation/fix dispatch additionally requires `DISH_IMPLEMENTATION_CLAIM_URL` and `DISH_IMPLEMENTATION_CLAIM_TOKEN`; when an Implementation consumer is configured but that global guard is unavailable, dispatch fails closed before writing an Implementation/fix lease or launching the consumer.

## Derived lifecycle states

The JSON schema is `dish-pr-lifecycle-status-v1`. The state engine distinguishes:

- `authoring_implementation_in_progress`;
- `implementation_continuation_required`;
- `review_ready`;
- `review_in_progress`;
- `changes_requested_fix_in_progress`;
- `review_passed_evaluating_gates`;
- `local_implementation_completion_required`;
- `local_certification_required`;
- `waiting_ci_certification`;
- `waiting_external_dependency`;
- `integration_ready`;
- `merging_integration_in_progress`;
- `merged`;
- `closed_superseded`.

A draft PR that explicitly carries `IMPLEMENTATION EVIDENCE PENDING: <evidence>` is `IMPLEMENTATION CONTINUATION REQUIRED`, not Review/Integration/local certification. The dispatcher reuses the existing Implementation/fix consumer. If no current `phase=implementation`/`fix` owner is active, it first writes a durable `dish-implementation-continuation:v1` handoff on the same PR, then claims `phase=implementation` and dispatches the exact existing branch/task/head plus named evidence. That comment is the explicit replacement-owner handoff when the prior Implementation agent is unavailable. The consumer must follow the single canonical repository handoff contract at `dish/docs/agents/templates/implementation-handoff.md` and reconcile the matching local `tools/agent-worktree` claim before touching preserved state; the PR handoff/lease does not itself override a live local claim. The only human-facing message for this state is `PR #N still needs Implementation to finish <evidence>.`

`VERDICT: MERGE` is not terminal. It starts Integration gate evaluation. Ordinary Review does not wait for ordinary CI and does not require a pre-review sync to moved `main` unless that movement creates a known semantic dependency that invalidates the review question.

`scripts/pr_gate.py` diagnoses the exact reviewed head as `PASS`, `PENDING`, `FAILED_REQUIRED_CI`, `EVIDENCE_MISSING_OR_STALE`, `HEAD_MOVED`, or (for transport/read failures distinguished by the lifecycle adapter) `INFRASTRUCTURE_ERROR`. Only `PENDING` is `WAITING CI / CERTIFICATION`. Missing/stale evidence remains fail-closed while accurately staying in gate evaluation; it is not described as CI still running. Failed required CI is either PR-owned and returned to Implementation/fix, or externally owned only when a valid durable external-dependency record proves that ownership.

## Structured advisory leases

Active agent work uses exact-head PR comment leases:

```text
<!-- dish-agent-lease:v1 phase=review head=<40-char-sha> lease=<uuid> -->
```

The dispatcher may add `owner=` and `class=` fields. Supported phases include `implementation`, `fix`, `review`, and `integration`.

Lease rules:

- advisory only; never semantic, Integration, or local-agent ownership authority;
- exact-head scoped;
- a head move invalidates immediately;
- stale 60 minutes after the most recent structured renewal/activity for that lease UUID;
- a formal exact-head Review supersedes a `phase=review` lease;
- merge/close invalidates every lease;
- domain-deep scrutiny stays inside the active Review lease; only genuinely different external-expert parallel Review/evidence may use a separate lease;
- restart reconstructs active leases from PR comments.

Explicit release is:

```text
<!-- dish-agent-lease-release:v1 lease=<uuid> -->
```

A renewal repeats the lease marker with the same UUID on a new PR comment. Do not infer local-agent liveness or stale-owner eligibility from a PR lease, its age, an agent process/session, or a GitHub assignee. Local Implementation/fix exclusivity and stale-owner transfer are enforced by the repository-owned global Implementation claim plus subordinate `tools/agent-worktree` locks under the single canonical handoff contract at `dish/docs/agents/templates/implementation-handoff.md`.

Before launching an Implementation/fix consumer, the dispatcher queries the global claim guard for the single owning Asana task. A writable/unsynchronized current generation is not dispatchable merely because the task/PR presentation is stale. No existing generation permits a fresh acquisition; `review-ready`/`released` lineage is passed to the consumer as the exact takeover CAS input after branch/PR/head agreement is verified. Formal exact-head `BLOCK` is an explicit lifecycle handoff, but a replacement still performs exact-generation takeover; draft-authoring continuation must not infer cross-host staleness from an expired advisory PR lease. Claim-service/readback unavailability or lineage disagreement fails closed.

## Review routing

The default ordinary route is `substantive`. A durable explicit route may be placed in the PR body as `REVIEW CLASS: <class>` or in a PR comment:

```text
<!-- dish-review-route:v1 head=<sha> class=<class> -->
```

Classes are `light`, `focused`, `mechanical`, `substantive`, or `domain:<name>`. `domain:<name>` means the ordinary Review Workspace Agent must deepen scrutiny for that domain inside the same formal Review workflow. Legacy durable `specialist:<name>` markers normalize to `domain:<name>` and do not select another generic AI reviewer. A prior exact-head `BLOCK` whose return contract says `FOCUSED RECHECK`, `MECHANICAL CHECK ONLY`, or `DOMAIN DEEP RECHECK` supplies the bounded next review class after a new head appears. Ambiguous work defaults to `substantive`.

For `light`, `focused`, or `mechanical`, `DISH_LOCAL_REVIEW_COMMAND` may provide a bounded local reviewer. It receives the lifecycle JSON on standard input. The local adapter is never the default semantic reviewer.

Ordinary substantive Review prefers a published ChatGPT Review Workspace Agent. Configure:

- `DISH_WORKSPACE_AGENT_ACCESS_TOKEN` — Workspace Agent access token;
- `DISH_REVIEW_API_TRIGGER_ID` — published Review API trigger ID.

The adapter calls that one Workspace Agents Review trigger for both ordinary substantive and domain-deep Review, with the exact PR URL/number, exact current head, review class, owning Asana task identity, and instruction to follow `dish/docs/agents/review.md`. Its `Idempotency-Key` is deterministically derived from repository + PR + exact head + review class. A domain class changes review depth, not reviewer identity. Agent-chat output is never review completion; only the single formal exact-head GitHub `COMMENT` review with `VERDICT: MERGE` or `VERDICT: BLOCK` advances semantic Review state.

If the required token or published trigger is unavailable, the dispatcher reports that exact configuration boundary. It does not silently substitute Claude/Codex as the semantic reviewer.

## BLOCK -> implementation/fix routing

A formal exact-head `VERDICT: BLOCK` is not only a status classification. A `FAILED_REQUIRED_CI` diagnosis without a valid active external-dependency record is also PR-owned implementation/fix work even when the exact-head semantic Review verdict remains `MERGE`. `dispatch` routes either condition to the configured existing implementation/fix consumer. Configure that consumer with:

```sh
DISH_IMPLEMENTATION_FIX_COMMAND='<existing implementation/fix launcher>'
```

or `--implementation-fixer`. The command receives `dish-pr-fix-dispatch-v1` JSON on standard input containing the exact PR URL/number, branch, blocked head SHA, owning task IDs, the current lifecycle snapshot, and either the authoritative formal BLOCK review or the structured PR-owned CI diagnosis. The consumer must follow `dish/docs/agents/implementation.md` and the single canonical handoff contract at `dish/docs/agents/templates/implementation-handoff.md`, reconcile the matching `tools/agent-worktree` claim before touching local state, update only the authorized existing PR branch, and re-read GitHub before semantic work. Matching Asana task identity on another branch/PR is not authority. A CI-driven semantic fix changes head identity and therefore requires substantive Review on the new head.

Before launching the consumer, the dispatcher writes an exact-head `phase=fix` advisory lease. A fresh `phase=fix` or `phase=implementation` lease on the current blocked head prevents duplicate dispatcher launches, but does not transfer or override a local `tools/agent-worktree` claim. A head move immediately invalidates the old review and lease; the dispatcher never launches a fix consumer for a BLOCK that is no longer on the current head. If the configured command fails synchronously, the dispatcher releases its lease so recovery is not deadlocked.

Missing implementation/fix consumer configuration is a deployment boundary, not a request for Marco to forward the review transcript. The durable BLOCK review remains on GitHub until the consumer is configured/recovered.


## External dependency blockers

A failed exact-head required check may enter `WAITING ON EXTERNAL DEPENDENCY` only when the PR carries a valid durable dependency record matching that exact check. The marker is a GitHub PR comment and is restart-reconstructable:

```text
<!-- dish-external-dependency:v1 action=blocked task=<16-digit-gid> pr=<owner-pr> check=<percent-encoded-check> main=<40-char-main-sha> evidence=<percent-encoded-reference> reason=<percent-encoded-reason> -->
```

`pr=` is optional when no code PR owns the fix. `task=`, `check=`, `main=`, and `evidence=` are mandatory. Values that may contain spaces are percent-encoded. Malformed records never authorize external ownership. Records are ordered deterministically by comment timestamp, numeric comment id, then marker order; the newest valid record wins. Resolution/supersession uses the same full identity with `action=resolved`, or is observed from a merged owner PR / completed owner task when that authority is available. A closed-unmerged owner PR remains blocked explicitly.

While externally blocked the dispatcher does not launch Implementation/fix or local-certification work for the blocked PR and does not merge it. Human output is only the concise owner/check status. Once the owner resolves, the target PR returns to exact-head evidence evaluation; it does not inherit or skip CI/Review.

## Local work after Review MERGE

A formal Review must keep using its required `TESTS TO RUN:` line. A non-`NONE` command creates `LOCAL CERTIFICATION REQUIRED` until the exact head has a durable completion marker:

```text
<!-- dish-local-completion:v1 kind=certification head=<sha> result=pass -->
```

When Review identifies a genuinely required local implementation-completion action without yet changing source, it records:

```text
LOCAL IMPLEMENTATION COMPLETION REQUIRED: <exact action>
```

Completion is recorded as:

```text
<!-- dish-local-completion:v1 kind=implementation head=<sha> result=complete -->
```

Before notifying Marco about either local action, the dispatcher first writes the complete exact-head handoff to the PR with a `dish-local-handoff:v1` marker. For `kind=implementation`, the local worker must follow the single canonical handoff contract at `dish/docs/agents/templates/implementation-handoff.md` and reconcile the matching agent-worktree claim before touching prepared state. If the local implementation action changes the source head, the prior Review and completion marker are stale and the new head returns to Review/recheck under the normal rules.

For reviewed exact heads with a complete durable certification handoff, bounded Integration can execute that handoff locally instead of turning it into a Marco message. Configure the local Integration consumer with:

```sh
DISH_LOCAL_INTEGRATION_CERTIFICATION_COMMAND='<local Integration launcher>'
```

or `--local-integration-certifier`. The command receives `dish-pr-integration-certification-v1` JSON on standard input only after the dispatcher has written and re-read the durable exact-head handoff. The payload contains repository/PR identity, branch and exact reviewed head, the complete PR body, owning task IDs, the formal exact-head Review, the certification handoff, and the current lifecycle snapshot. The consumer acts under `dish/docs/agents/integration.md`: it re-reads live GitHub/Asana authority, executes the durable handoff, derives existing routine task/branch/agent IDs or safely creates the documented attempt identities, and records durable exact-head pass/fail evidence. It must not ask Marco to copy routine identifiers or choose a bypass that the workflow can resolve safely.

A synchronous consumer return without a durable completion marker leaves `LOCAL CERTIFICATION REQUIRED` with a machine-actionable residual reason; it does not emit a human-action notice. A durable pass is re-read and the dispatcher continues the ordinary CI/order/mergeability/Integration gates in the same dispatch when possible. A durable failure is evidence for the normal Implementation/fix loop, not permission for Integration to change semantics. Missing Integration authority or a missing local Integration consumer remains an execution/authority boundary and may still require a real human action when no authorized execution path exists.

## Integration composition

All local work must be complete and `scripts/pr_gate.py integration` must pass on the exact reviewed head before the dispatcher can enter Integration.

Tool capability does not grant Integration authority. Bounded dispatcher composition is explicitly enabled only by `--integration-authority` or:

```sh
DISH_INTEGRATION_AUTHORITY=bounded-reviewed-head
```

With that authority and merge capability, the dispatcher:

1. re-reads the PR/current head;
2. re-evaluates exact-head Review, local work, mergeability/order, ordinary CI, and certification;
3. creates/resumes an exact-head `phase=integration` lease;
4. merges with expected-head protection;
5. re-reads GitHub;
6. reports `MERGED` only when authoritative PR state is merged.

The composed Integration path is mechanical only. It cannot make semantic fixes, weaken tests, resolve semantic conflicts, or give Review Implementation authority. If all gates are green but authorization/capability is unavailable, state is `INTEGRATION READY` with the exact residual reason.

## Human notifications

Routine queue movement is silent. Human messages are limited to a real local action/decision or useful terminal result. The dispatcher records an exact-head `dish-human-notice:v1` idempotency marker before emitting a human-action notice, so repeated polls do not repeat the same notice. For local work, the complete `dish-local-handoff:v1` comment is written and re-read before that notice marker or human message. Detailed handoff remains on the PR.
