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

GitHub authentication comes from `GITHUB_TOKEN` or `GH_TOKEN`. `ASANA_ACCESS_TOKEN` is optional for linked-task metadata; missing Asana access is surfaced rather than replaced by local state.

## Derived lifecycle states

The JSON schema is `dish-pr-lifecycle-status-v1`. The state engine distinguishes:

- `authoring_implementation_in_progress`;
- `review_ready`;
- `review_in_progress`;
- `changes_requested_fix_in_progress`;
- `review_passed_evaluating_gates`;
- `local_implementation_completion_required`;
- `local_certification_required`;
- `waiting_ci_certification`;
- `integration_ready`;
- `merging_integration_in_progress`;
- `merged`;
- `closed_superseded`.

`VERDICT: MERGE` is not terminal. It starts gate evaluation.

## Structured advisory leases

Active agent work uses exact-head PR comment leases:

```text
<!-- dish-agent-lease:v1 phase=review head=<40-char-sha> lease=<uuid> -->
```

The dispatcher may add `owner=` and `class=` fields. Supported phases include `implementation`, `fix`, `review`, and `integration`.

Lease rules:

- advisory only; never semantic or Integration authority;
- exact-head scoped;
- a head move invalidates immediately;
- stale 60 minutes after the most recent structured renewal/activity for that lease UUID;
- a formal exact-head Review supersedes a `phase=review` lease;
- merge/close invalidates every lease;
- intentional specialist/deep parallel Review may use a separate lease;
- restart reconstructs active leases from PR comments.

Explicit release is:

```text
<!-- dish-agent-lease-release:v1 lease=<uuid> -->
```

A renewal repeats the lease marker with the same UUID on a new PR comment. Do not infer liveness from an agent process, session, or GitHub assignee.

## Review routing

The default ordinary route is `substantive`. A durable explicit route may be placed in the PR body as `REVIEW CLASS: <class>` or in a PR comment:

```text
<!-- dish-review-route:v1 head=<sha> class=<class> -->
```

Classes are `light`, `focused`, `mechanical`, `substantive`, or `specialist:<name>`. A prior exact-head `BLOCK` whose return contract says `FOCUSED RECHECK` or `MECHANICAL CHECK ONLY` also supplies the bounded next review class after a new head appears. Ambiguous work defaults to `substantive`.

For `light`, `focused`, or `mechanical`, `DISH_LOCAL_REVIEW_COMMAND` may provide a bounded local reviewer. It receives the lifecycle JSON on standard input. The local adapter is never the default semantic reviewer.

Ordinary substantive Review prefers a published ChatGPT Review Workspace Agent. Configure:

- `DISH_WORKSPACE_AGENT_ACCESS_TOKEN` — Workspace Agent access token;
- `DISH_REVIEW_API_TRIGGER_ID` — published Review API trigger ID;
- `DISH_SPECIALIST_TRIGGER_IDS` — optional JSON mapping such as `{"postgresql":"agtch_..."}`.

The adapter calls the Workspace Agents trigger API with the exact PR URL/number, exact current head, review class, owning Asana task identity, and instruction to follow `dish/docs/agents/review.md`. Its `Idempotency-Key` is deterministically derived from repository + PR + exact head + review class. Agent-chat output is never review completion; only the formal exact-head GitHub `COMMENT` review with `VERDICT: MERGE` or `VERDICT: BLOCK` advances semantic Review state.

If the required token or published trigger is unavailable, the dispatcher reports that exact configuration boundary. It does not silently substitute Claude/Codex as the semantic reviewer.

## BLOCK -> implementation/fix routing

A formal exact-head `VERDICT: BLOCK` is not only a status classification. `dispatch` routes it to the configured existing implementation/fix consumer. Configure that consumer with:

```sh
DISH_IMPLEMENTATION_FIX_COMMAND='<existing implementation/fix launcher>'
```

or `--implementation-fixer`. The command receives `dish-pr-fix-dispatch-v1` JSON on standard input containing the exact PR URL/number, branch, blocked head SHA, owning task IDs, the authoritative formal BLOCK review, and the current lifecycle snapshot. The consumer must follow `dish/docs/agents/implementation.md`, update the existing PR branch, and re-read GitHub before semantic work.

Before launching the consumer, the dispatcher writes an exact-head `phase=fix` lease. A fresh `phase=fix` or `phase=implementation` lease on the current blocked head prevents duplicate dispatch. A head move immediately invalidates the old review and lease; the dispatcher never launches a fix consumer for a BLOCK that is no longer on the current head. If the configured command fails synchronously, the dispatcher releases its lease so recovery is not deadlocked.

Missing implementation/fix consumer configuration is a deployment boundary, not a request for Marco to forward the review transcript. The durable BLOCK review remains on GitHub until the consumer is configured/recovered.

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

Before notifying Marco about either local action, the dispatcher first writes the complete exact-head handoff to the PR with a `dish-local-handoff:v1` marker. If the local implementation action changes the source head, the prior Review and completion marker are stale and the new head returns to Review/recheck under the normal rules.

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
