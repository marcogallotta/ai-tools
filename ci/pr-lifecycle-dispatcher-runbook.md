# PR lifecycle dispatcher runbook — historical

> **NEVER ACTIVATED — historical/reference only.**
>
> The dispatcher/controller code was built but never deployed, commissioned, or used as standing
> Dish infrastructure, and it will not be activated. Current PR discovery, classification, role
> handoff, fix continuation, and Integration handoff are manual actions performed by the acting role
> under `dish/docs/agents/`. Commands, environment variables, markers, and present-tense behavioral
> descriptions below record the abandoned prototype design only. Do not execute them as the current
> operating procedure or assume a background process will continue work.

## Navigation

- [Authority and recovery model](#authority-and-recovery-model)
- [Commands](#commands) and [derived lifecycle states](#derived-lifecycle-states)
- [Terminal disposition and cleanup](#terminal-disposition-and-cleanup) and [structured advisory leases](#structured-advisory-leases)
- [Review routing](#review-routing) and [BLOCK → implementation/fix routing](#block--implementationfix-routing)
- [External dependency blockers](#external-dependency-blockers)
- [Local work after Review MERGE](#local-work-after-review-merge)
- [Integration composition](#integration-composition)
- [Human notifications](#human-notifications)

## Authority and recovery model

The prototype `scripts/pr_lifecycle.py` was designed to derive a queue view from durable GitHub PR state and the linked Asana task identity without an authoritative local database. It has never been the current lifecycle observer. Manual recovery starts with fresh GitHub/Asana reads by the acting role.

The exact PR source head SHA is the lifecycle identity for Review, leases, local-work handoff, certification, and Integration. A head change immediately invalidates exact-head reviews, leases, local-work completion markers, and certification markers from the older head.

The prototype reused `scripts/pr_gate.py` for ordinary Review discoverability and exact-head ordinary-CI / Integration gate semantics. The shared gate predicate remains current; this abandoned controller does not.

## Commands

Historical prototype commands (retained for reference; do not run as the current operating procedure):

```sh
python scripts/pr_lifecycle.py status --format json
python scripts/pr_lifecycle.py status --format table
python scripts/pr_lifecycle.py dispatch --format table
python scripts/pr_lifecycle.py watch --interval 180
python scripts/pr_lifecycle.py watch --dispatch --interval 180
```

`status`, `dispatch`, and `watch` described the prototype interface. No standing process invokes them.

GitHub authentication comes from `GITHUB_TOKEN` or `GH_TOKEN`. `ASANA_ACCESS_TOKEN` is optional for linked-task metadata; missing Asana access is surfaced rather than replaced by local state.

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

A draft PR that explicitly carries `IMPLEMENTATION EVIDENCE PENDING: <evidence>` remains `IMPLEMENTATION CONTINUATION REQUIRED`, not Review/Integration/local certification. Under the current manual procedure, the acting Coordinator or Implementation owner re-reads the exact PR/task/head and persists any replacement-owner handoff explicitly; no dispatcher writes or consumes that handoff.

`VERDICT: MERGE` is not terminal. It starts Integration gate evaluation. Ordinary Review does not wait for pre-Review CI and does not require a pre-review sync to moved `main` unless that movement creates a known semantic dependency that invalidates the review question.

## Terminal disposition and cleanup

The prototype would have included terminal handling in `dispatch` / `watch --dispatch`. Current terminal reconciliation and cleanup are manual, use authoritative GitHub/Asana disposition, and preserve the same recoverability guards. Age, inactivity, stale leases, parking, and temporary blockers never create terminal authority.

For an open PR, manual close requires current linked Asana authority that explicitly marks the owning task `SUPERSEDED`, `ABANDONED`, or `REPLACED`, or explicitly names that exact `PR #N` as superseded/abandoned/not-to-be-revived. Generic task completion is insufficient. Before closing, the acting authorized role writes and re-reads the durable disposition required by current standing policy, then requires authoritative GitHub closed-state readback.

For `MERGED`, `CLOSED`, or an explicitly closed `superseded`/`abandoned` lineage, current cleanup is manual and recoverability-first. The historical prototype would have:

1. accept only `agent/*` source branches and refuse a GitHub-protected branch;
2. call repository-owned `tools/agent-worktree cleanup` with task, PR number, branch, exact terminal head, and disposition;
3. require exact remote-head match before remote deletion;
4. preserve dirty, ignored, unpublished-only, ambiguous, moved, or reused local state instead of forcing cleanup;
5. conditionally remove the registered worktree and exact local branch when a matching task record exists;
6. delete the exact remote agent branch with expected-head protection and verify readback;
7. retain local `dish-terminal-cleanup-v1` journal/history when local state exists; and
8. write and re-read a `dish-terminal-cleanup:v1` PR marker only after cleanup succeeds.

If the process dies between steps, the local cleanup journal plus Git/worktree/remote readback makes the next pass idempotently continue from the already-completed step. A cleanup refusal leaves the PR terminal but records a concise recovery anomaly; it never reopens or force-deletes recovery state.

`scripts/pr_gate.py` diagnoses the exact reviewed head as `PASS`, `PENDING`, `FAILED_REQUIRED_CI`, `EVIDENCE_MISSING_OR_STALE`, `HEAD_MOVED`, or (for transport/read failures distinguished by the lifecycle adapter) `INFRASTRUCTURE_ERROR`. Only `PENDING` after an exact-head `VERDICT: MERGE` is `REVIEW PASSED / CERTIFICATION PENDING`; successful semantic Review remains explicit while certification runs. Missing/stale evidence remains fail-closed while accurately staying in gate evaluation; it is not described as CI still running. A failed required gate remains visibly failed while ownership is separately classified as `PR_OWNED`, `LIKELY_NON_PR_OWNED`, `PROVEN_CURRENT_MAIN`, `INFRASTRUCTURE`, or `AMBIGUOUS`. Likely non-PR ownership can avoid an unnecessary candidate fix/rerun but cannot authorize final landing through red CI. Final landing is possible only through the typed `MERGEABLE_WITH_BASELINE_DEBT` boundary: exact-main reproduction, candidate-specific exclusion evidence, exact-head Review, semantic non-interaction, and an active exact-head-bound repair owner. Newer contradictory exact-head ownership evidence overrides stale baseline markers.

The normal hosted gate is `.github/workflows/ci.yml` on formal `pull_request_review` submission. Its candidate identity is the Review `commit_id`, not workflow `head_sha`. A planner step computes exact merge-base changed paths and required execution groups before the single conditional runner job is allocated. A `pull_request` `synchronize` event exists only to cancel a superseded in-flight certification via concurrency; it does not allocate heavy work. The accepted `Dish / exact-head certification` status must target the exact Actions run and be fresher than the formal Review and current rerun attempt. Periodic full regression is separate and cannot satisfy this gate.

## Structured advisory leases

Active agent work uses exact-head PR comment leases:

```text
<!-- dish-agent-lease:v1 phase=review head=<40-char-sha> lease=<uuid> -->
```

The historical prototype allowed `owner=` and `class=` fields. Current roles may still use advisory leases where their standing contracts say so; this runbook is not their authority.

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

A renewal repeats the lease marker with the same UUID on a new PR comment. Do not infer local-agent liveness or stale-owner eligibility from a PR lease, its age, an agent process/session, or a GitHub assignee. Local Implementation/fix exclusivity and stale-owner transfer are enforced by `tools/agent-worktree` claims under the single canonical handoff contract at `dish/docs/agents/templates/implementation-handoff.md`.

## Review routing

This section records the abandoned automatic routing design. Current Review routing is manual under
`dish/docs/agents/review.md#Review claims and manual routing`.

The default ordinary route is `substantive`. A durable explicit route may be placed in the PR body as `REVIEW CLASS: <class>` or in a PR comment:

```text
<!-- dish-review-route:v1 head=<sha> class=<class> -->
```

Classes are `light`, `focused`, `mechanical`, `substantive`, or `domain:<name>`. `domain:<name>` means the ordinary Review Workspace Agent must deepen scrutiny for that domain inside the same formal Review workflow. Legacy durable `specialist:<name>` markers normalize to `domain:<name>` and do not select another generic AI reviewer. A prior exact-head `BLOCK` whose return contract says `FOCUSED RECHECK`, `MECHANICAL CHECK ONLY`, or `DOMAIN DEEP RECHECK` supplies the bounded next review class after a new head appears. Ambiguous work defaults to `substantive`.

For `light`, `focused`, or `mechanical`, `DISH_LOCAL_REVIEW_COMMAND` may provide a bounded local reviewer only when the exact current head has a positive implementation-host witness for `CHATGPT_IMPLEMENTATION`. The witness uses `<!-- dish-implementation-host-witness:v1 head=<sha> host=chatgpt source=orchestration launcher=<id> -->`. Missing, ambiguous, local, or post-PR-unproven Implementation provenance routes to ChatGPT Review. The local adapter is never the default semantic reviewer.

Ordinary substantive Review prefers a published ChatGPT Review Workspace Agent. Configure:

- `DISH_WORKSPACE_AGENT_ACCESS_TOKEN` — Workspace Agent access token;
- `DISH_REVIEW_API_TRIGGER_ID` — published Review API trigger ID.

The adapter calls that one Workspace Agents Review trigger for both ordinary substantive and domain-deep Review, with the exact PR URL/number, exact current head, review class, owning Asana task identity, and instruction to follow `dish/docs/agents/review.md`. Its `Idempotency-Key` is deterministically derived from repository + PR + exact head + review class. A domain class changes review depth, not reviewer identity. Agent-chat output is never review completion; only the single formal exact-head GitHub `COMMENT` review with `VERDICT: MERGE` or `VERDICT: BLOCK` advances semantic Review state.

The proposed automatic adapter would have reported a missing token or published trigger as a configuration boundary. No such trigger is part of the current manual route.

## BLOCK -> implementation/fix routing

This section records the abandoned automatic fix-consumer design. Current formal-BLOCK continuation
uses the manual Worker procedure or an explicit current Implementation handoff.

A formal exact-head `VERDICT: BLOCK` remains durable fix input. The `dispatch` command and consumer configuration below were never activated; the acting Worker or Coordinator instead binds the exact live task/PR/branch/head/review identity manually before any fix.

```sh
DISH_CHATGPT_IMPLEMENTATION_FIX_COMMAND='<hosted Implementation launcher>'
DISH_LOCAL_IMPLEMENTATION_FIX_COMMAND='<local Implementation launcher>'
```

Legacy `DISH_IMPLEMENTATION_FIX_COMMAND` / `--implementation-fixer` remains accepted only when `DISH_IMPLEMENTATION_FIX_HOST=chatgpt|local` classifies it exactly; otherwise fix dispatch fails closed. The selected command receives `dish-pr-fix-dispatch-v1` JSON on standard input containing `implementation_host`, the exact PR URL/number, branch, blocked head SHA, owning task IDs, the current lifecycle snapshot, and either the authoritative formal BLOCK review or the structured PR-owned CI diagnosis. The consumer must follow `dish/docs/agents/implementation.md` and the single canonical handoff contract at `dish/docs/agents/templates/implementation-handoff.md`, reconcile the matching `tools/agent-worktree` claim before touching local state, update only the authorized existing PR branch, and re-read GitHub before semantic work. Matching Asana task identity on another branch/PR is not authority. A CI-driven semantic fix changes head identity and therefore requires substantive Review on the new head.

The current manual fix owner may post an advisory `phase=fix` lease, then must re-read the PR, branch, head, formal Review or CI ownership, and live task authority. Formal BLOCK eligibility is exact `(head, block_review_id)`; pre-BLOCK authoring leases/state and older Review rounds are ineligible. PR-owned CI failure may enter the same route only after failure ownership is durably `PR_OWNED`; current-main, infrastructure, or ambiguous failures do not mutate the candidate.

Immediately before consumer dispatch and publication, re-read those exact identities. Any authority movement produces zero semantic mutation. Local `tools/agent-worktree` ownership and non-force expected-head publication remain mandatory.

Missing implementation/fix consumer configuration is a deployment boundary, not a request for Marco to forward the review transcript. The durable BLOCK review remains on GitHub until the consumer is configured/recovered.


## External dependency blockers

A failed exact-head required check may enter `WAITING ON EXTERNAL DEPENDENCY` only when the PR carries a valid durable dependency record matching that exact check. The marker is a GitHub PR comment and is restart-reconstructable:

```text
<!-- dish-external-dependency:v1 action=blocked task=<16-digit-gid> pr=<owner-pr> check=<percent-encoded-check> head=<40-char-candidate-sha> main=<40-char-main-sha> fingerprint=<64-char-causal-fingerprint> evidence=<percent-encoded-reference> reason=<percent-encoded-reason> -->
```

`pr=` is optional when no code PR owns the fix. `task=`, `check=`, `main=`, and `evidence=` are mandatory. New baseline-debt landing records also require exact `head=` and the shared causal `fingerprint=`; legacy records without them remain waiting-only evidence. The separate exact-head `dish-ci-failure-ownership:v1` record carries the classification, candidate disposition, workflow `generation=run-<id>-attempt-<n>`, causal basis/contrary evidence, repair owner, main reproduction, candidate-specific evidence, and semantic-interaction result. Values that may contain spaces are percent-encoded. Malformed or stale-generation records never authorize external ownership or landing. Records are ordered deterministically by comment timestamp, numeric comment id, then marker order; the newest valid record wins. Resolution/supersession uses the same full identity with `action=resolved`, or is observed from a merged owner PR / completed owner task when that authority is available. A closed-unmerged owner PR remains blocked explicitly and requires a replacement repair owner.

While externally blocked, the acting role does not launch Implementation/fix or local-certification work for the blocked PR and does not merge it. Once the owner resolves, the target PR returns to exact-head evidence evaluation; it does not inherit or skip CI/Review.

## Local work after Review MERGE

New formal MERGE reviews use the phase-explicit pair `PRE-INTEGRATION TESTS TO RUN:` and `POST-MERGE GATES:`. Only a non-`NONE` `PRE-INTEGRATION TESTS TO RUN` command creates `LOCAL CERTIFICATION REQUIRED` before source Integration. `POST-MERGE GATES` is carried as residual acceptance metadata and does not block source merge by itself. If either new-format field appears, both must appear; partial metadata fails closed as malformed Review metadata rather than becoming CI pending or a privileged local handoff. Legacy exact-head reviews containing only `TESTS TO RUN:` retain the existing pre-Integration behavior.

A required pre-Integration command remains pending until the exact head has a durable completion marker:

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

Before notifying Marco about either local action, the acting role first writes and re-reads the complete exact-head handoff on the PR. For `kind=implementation`, the local worker follows the canonical handoff contract. If the local implementation action changes the source head, the prior Review and completion marker are stale and the new head returns to Review/recheck under the normal rules.

The prototype proposed one local Integration consumer for certification and final V1-A landing:

```sh
DISH_LOCAL_INTEGRATION_COMMAND='<local Claude/Codex Integration launcher>'
```

The `--local-integration-launcher` and legacy `--local-integration-certifier` spellings were never activated. The intended command payloads were `dish-pr-integration-certification-v1` and `dish-pr-local-integration-v1`; they are historical protocol notes, not a current launch path.

A synchronous certification return without a durable completion marker leaves `LOCAL CERTIFICATION REQUIRED` with a machine-actionable residual reason. A durable pass is re-read and the candidate advances through the remaining exact-head gates. A durable failure returns to the normal Implementation/fix path. Missing local launcher capability is not replaced by ChatGPT, connector, Actions, or broker landing.

## Integration composition

This section records the abandoned automatic Integration-composition design. Current Integration
requires an explicit manual exact-head handoff and follows
`dish/docs/agents/integration.md#Manual Integration handoff`.

All required local certification must be complete and `scripts/pr_gate.py integration` must pass on the exact reviewed head before ordinary landing. A reviewed head blocked only by mechanically resolvable base/mergeability/order movement may also be handed to the same local Integration consumer; semantic ambiguity still returns to Implementation.

Tool capability does not grant Integration authority. The abandoned design proposed enabling bounded dispatcher composition with `--integration-authority` or:

```sh
DISH_INTEGRATION_AUTHORITY=bounded-reviewed-head
```

The never-activated design would have made the dispatcher a deterministic classifier/handoff controller only. It would have:

1. re-read the PR/current head, exact formal Review, certification, mergeability/order, and current target branch;
2. write and re-read one durable `dish-local-integration-handoff:v1` record binding repository, PR, branch, exact reviewed head, exact Review id, and observed target-branch SHA;
3. acquire a local per-PR/head `fcntl` fence under `~/.local/state/dish/integration/`;
4. invoke only `DISH_LOCAL_INTEGRATION_COMMAND` while holding that fence;
5. re-read GitHub after the child returned and stop on any new PR head; and
6. enter post-merge reconciliation only after authoritative `MERGED` readback.

The local Integration child owns the irreversible boundary. It must use a live checkout and real Git/worktree tooling, fetch current origin state, run literal required PRE-INTEGRATION evidence, and re-read live GitHub plus the explicit owning Asana task before its first mutation and again immediately before merge. It may perform only conflict-free/mechanical reconciliation already determined by authority. Any content-changing reconciliation creates a new exact PR head and stops for fresh Review. Final merge must use expected-head/current-state protection and authoritative GitHub MERGED readback.

The child checkpoints durable recovery state with:

```sh
python scripts/pr_lifecycle.py integration-checkpoint \
  --claim-path <claim.json> \
  --claim-id <claim-id> \
  --phase <certifying|reconciling|reconciled|premerge|head-changed|failed-evidence|merged>
```

A replacement local Integration execution may proceed only after it can acquire the same OS fence; it then receives the prior JSON checkpoint as recovery context. Two starts for the same PR/head therefore cannot both own consequential mutation. Advisory `phase=integration` comments remain visibility only and never admission.

If the local launcher/laptop is unavailable, the PR remains `INTEGRATION READY` with zero merge mutation. There is deliberately no remote ChatGPT, connector-native, or GitHub Actions fallback for V1-A.

## Human notifications

Current human messaging follows the role contracts: notify only for a real local action/decision or useful terminal result, preserve explicit Review PASS/BLOCK, and classify local residuals as `TESTS ONLY`, `IMPLEMENTATION / PUBLICATION`, or `LOCAL SYSTEM ACCESS`. The automatic notice-marker behavior described here was never activated.

## Pre-Review installed-host Implementation continuation

A candidate that changes an active repository-owned hook, `.claude/settings.json`, `codex/hooks.json`, or repository install wiring is automatically gated on `exact-head installed Claude/Codex host certification`. The gate is derived from the exact PR changed-file surface, not from a human checklist line; removing draft status or deleting an `IMPLEMENTATION EVIDENCE PENDING` line does not bypass it. An explicit different authoring-evidence line is finished first, after which the host gate remains.

This remains `IMPLEMENTATION CONTINUATION REQUIRED`; it is not `LOCAL CERTIFICATION REQUIRED` and does not add a lifecycle state. The current acting role manually writes and verifies the exact-head continuation handoff before a local Implementation owner accepts it.

The local worker—not Marco—owns the routine real-host loop: re-read live lineage; consume fresh `tools/agent-worktree claim --require-launch-provenance` identity; capture installed versions plus effective config/symlink pre-state; fence all affected shared host-config producers/consumers for the full mutation/test/restore window (or use genuinely isolated host state); activate only the exact candidate; drive the actual installed Claude/Codex loader/tool; diagnose and fix source on the same lineage; repeat after every new head; prove stale removed references are absent; then restore exact prior state or read back separately authorized final activation.

A passing result is durable only as an exact-head comment:

````text
<!-- dish-installed-host-cert:v1 head=<40-char-sha> result=pass hosts=<claude,codex subset> digest=<sha256> -->
INSTALLED HOST CERTIFICATE
```json
{ ... schema: "dish-installed-host-cert-v1" ... }
```
````

`scripts/installed_host_cert.py` defines the changed-surface classifier, canonical digest, certificate schema, and parser. The certificate binds the exact candidate/task/branch/head, fresh launch/claim identity, full-window fence pre/final digests, installed host versions/binaries, effective config sources, active path targets/digests, actual installed-loader execution, harmless governed action, deliberate conflict denial, recovery/shell-trust/stale-reference regressions, and restoration/final-activation readback. Any head movement makes previous evidence ineligible. Only a structurally valid exact-head pass permits normal independent Review; it grants no Review or Integration authority.

The local worker executes this boundary through the checked-in one-command surface documented in [`hook-certification.md`](hook-certification.md): `tools/dish-hook-certify --pr <n> --head <sha>`. The command owns host preflight, selected child launch, evidence collection, exact-byte/path validation, cleanup, durable certificate publication, and readback; the parent model does not reconstruct those mechanics from old PR transcripts.

## Worker execution profile

This section records an automated Workspace Agent path that was never commissioned. The current
manual Worker profile is `dish/docs/chatgpt-projects/worker.md`; it requires no dispatcher trigger,
provider run id, or automatic phase consumer.

Worker is one execution host/profile, never a union semantic role. Every trigger binds one exact standing role, one phase, and exact durable task/PR/head/design context. The Worker loads that role contract and cannot self-select or compose another specialist authority.

The proposed `DISH_WORKER_API_TRIGGER_ID` path was never activated. Its HTTP/admission rules below are historical design notes only.

A phase becomes active only after the exact trigger/config/kernel version produces its predetermined durable activation witness and that witness is authoritatively reread. Keep the existing legacy route for that phase until its smoke and first normal durable phase result succeed. Failure falls back per phase, not globally. Parallel executions are allowed on immutable/exact inputs; existing broker/CAS fencing applies only at real shared mutation boundaries. There is no global Worker lock, second scheduler, or queue. Integration landing remains outside Worker authority.
