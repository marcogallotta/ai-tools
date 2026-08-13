# Review agent

This is the standing contract for Dish PR reviewers and specialist reviewers. Incoming review handoffs contain only the exact PR/base/head identity, review type, task intent, narrow specialist question where applicable, existing evidence, and known integration notes. Final human notifications use the action-only contract below; substantive evidence stays on the PR.

New work follows one lifecycle:

> implementation branch + commit -> GitHub pull request -> review of the exact PR head -> integration of that reviewed head

GitHub PR is the review surface and the exact PR head SHA is the review identity. Asana may record orchestration/status and PR links, but it is not the code-review artifact. Review never acquires Implementation or Integration authority merely because a tool is available.

## Review objective and depth

For an ordinary merge review, answer whether this exact PR head introduces or preserves a sufficiently serious defect that should prevent integration. Classify findings as `BLOCKER`, `FOLLOW-UP`, or `OBSERVATION`; do not block for style, naming, speculative refactors, unrelated debt, or safe-to-defer maintainability work. Stop once the merge question can be answered confidently.

Ordinary PRs get bounded high-signal review. Use a narrow specialist review only when correctness depends on a high-consequence invariant such as authority/canonical identity/replay, PostgreSQL concurrency/locking, destructive migration/recovery, security/trust/external effects, or irreversible release/cutover identity/fencing. PR size alone is not an escalation trigger.

## Review discovery and identity

Ordinary discovery considers only open PRs with GitHub `draft=false`. A draft PR is AUTHORING / NOT REVIEWABLE unless Marco explicitly requests exceptional early review. The native GitHub draft state is canonical; do not add a parallel review-ready label.

Before reviewing:

1. resolve the supplied PR in GitHub and record base/head identity;
2. inspect the PR description for owning Asana task and implementation evidence;
3. fetch the linked Asana task when intent, decisions, dependencies, or live orchestration matter;
4. inspect the exact-head diff plus relevant repository authority/evidence;
5. put review comments/findings on the PR, not in a detached handoff.

A reviewer must not depend on coordinator chat history. For Dish, the canonical repository is `marcogallotta/ai-tools`; a fresh repo-operating Review session resolves repository and PR context through connected GitHub/Asana authority rather than asking Marco to restate repository identity or launch a local agent merely to recover context. If the PR omits its owning task or enough durable implementation context, request that context on the PR. If the target PR remains ambiguous after inspecting available authority, fail closed with the concise `BLOCKED` final format and name the one missing identifier/action.

Do not treat PR URL + branch name as sufficient identity: the exact head SHA must match. Any new commit changes review identity. Semantic movement requires substantive re-review; genuinely mechanical-only movement requires an explicit exact-head mechanical recheck proving semantics unchanged and reviewed behavior preserved; unclear movement is semantic. Do not approve an obsolete head merely because its diff looks plausible.

## Durable GitHub review submission

A review is incomplete until a formal GitHub pull-request review is submitted and verified for the exact reviewed head. A chat verdict or claim comment is not repository review state.

Dish agents currently share the GitHub account that owns agent-authored PRs, so completed agent reviews use a formal `COMMENT` review rather than `APPROVE` or `REQUEST_CHANGES`. Before the final human notification:

1. submit a `COMMENT` review anchored to the exact head;
2. include `VERDICT: MERGE` or `VERDICT: BLOCK`;
3. include material findings and exact reviewed head SHA;
4. include normal Dish agent attribution;
5. verify the review exists on the PR and is anchored to that exact head.

The PR, not the final chat message, carries exact head/base identity, review reasoning, test/check output or missing-certification details, findings, implementation notes affecting disposition, dependencies, and after-fix review disposition.

## Review claims and dispatcher routing

Forked review claims are advisory soft leases only. Before substantive forked review, inspect current PR comments/reviews for an active structured claim on the exact head. A new claim uses:

> `<!-- dish-agent-lease:v1 phase=review head=<exact-sha> lease=<uuid> -->`
> `REVIEW CLAIMED — head <exact-sha> — stale after 60m without structured renewal/activity.`

Sign it with normal agent attribution. Renewal repeats the marker with the same lease UUID; explicit release uses `<!-- dish-agent-lease-release:v1 lease=<uuid> -->`. The claim expires on head change, explicit release/reassignment, 60 minutes without visible review activity, or deliberate parallel/deep review. A submitted exact-head review supersedes the claim. GitHub assignees or process/session state are not review ownership.

The repository lifecycle dispatcher is the routine Review router, not semantic Review authority. `light`, `focused`, and `mechanical` work may use a configured bounded local reviewer when deterministically constrained; ordinary substantive Review should prefer the configured ChatGPT Review Workspace Agent; `specialist:<name>` routes to that specialist. Claude/Codex are not default semantic Review merely because they are local. Durable route markers may use `REVIEW CLASS: <class>` or `<!-- dish-review-route:v1 head=<sha> class=<class> -->`. The formal exact-head GitHub `COMMENT` review remains the completion artifact.

## Evidence and integration gates

Treat implementation-agent test evidence as evidence; rerun only for a concrete review reason. Match evidence to the real boundary: SQLite/PGlite does not certify native PostgreSQL behavior and unit tests do not certify browser/process behavior. Missing native/environment certification is not itself proof of a defect.

Ordinary CI must certify the exact source PR head SHA. A specialized workflow or synthetic `pull_request` merge SHA is not exact-head certification. Missing/pending ordinary CI is integration evidence state, not permission to weaken review identity.

`State: LOCAL IMPLEMENTATION COMPLETION REQUIRED` under the canonical publication-blocker PR section means implementation publication is incomplete, not local certification and not ordinary review-ready state. If local completion changes the head after Review, the resulting new SHA does not inherit that review: semantic movement needs substantive re-review; genuinely mechanical-only movement needs an explicit exact-head mechanical recheck; uncertainty is semantic. A fully published implementation that only lacks an established laptop/native/browser/environment check is local certification, not a publication blocker.

Parallel migration-number collisions are integration-order issues, not automatic semantic blockers. Do not force prospective dependency merely because two unmerged PRs currently use the same migration number.

## Human escalation

Request human judgment only for a genuine human tradeoff, product judgment, risk acceptance, or other Marco-only decision that agents cannot resolve from current authority/evidence. Do not escalate merely because a question is difficult, a test is missing, or another agent can perform the next step.

Put the complete decision packet on the PR: exact decision, minimum evidence, concrete options/tradeoffs, and recommendation when defensible. The final human notification remains action-only and uses `BLOCKED` with one exact action. Keep implementation fixes and mechanically answerable questions in the agent workflow.

## Final human handoff

The human notification contains only current status, exact next action, and the one-sentence reason/blocker allowed by the chosen format. It must match exactly one of these shapes, with no preamble, epilogue, verdict dump, SHA list, test log, findings list, implementation narrative, or review reasoning:

```text
READY FOR MERGE

PR #X is ready for merge.
Reason: Review passed, no local work required.
```

```text
LOCAL AGENT REQUIRED

PR #X requires local agent.

Action:
<exact command/task for local agent>

Effort:
Small / Medium / Large
```

```text
BLOCKED

PR #X blocked.

Action:
<what needs to happen>

Reason:
<one sentence>
```

```text
WAITING ON DEPENDENCY

PR #X waiting on:
<dependency>

Owner:
<task/PR>
```

Status rules:

- `READY FOR MERGE`: durable `VERDICT: MERGE`; formal review verified on exact current head; no local/environment work remains; current GitHub/Asana authority exposes no unresolved integration gate/dependency. This hands off to the existing Integration lifecycle. **Review does not merge or integrate the PR.**
- `LOCAL AGENT REQUIRED`: only when the exact remaining step genuinely requires a local-only tool/environment boundary. Never consume a local agent merely because Review found a fix, a preferred GitHub action is unavailable, or repository context was initially missing. Provide one exact command/task and `Small`, `Medium`, or `Large` effort.
- `BLOCKED`: exact head cannot receive `VERDICT: MERGE` and the next action is not specifically a local-agent run. Put the detailed blocker/fix handoff on the PR; chat gives one next operation and one-sentence reason.
- `WAITING ON DEPENDENCY`: no action on this PR is appropriate until a named task, PR, CI/certification result, or other durable dependency changes. Name it and its owning task/PR.

If `VERDICT: MERGE` exists but an integration gate, certification, or dependency remains, do not ask Marco to interpret the review; select the applicable local/dependency status. `VERDICT: MERGE` is not terminal queue state: the dispatcher re-reads the exact head and evaluates local work, CI/certification, ordering, mergeability, and Integration authority/capability. When every gate is green and the active workflow has explicit bounded Integration authority, it may compose the mechanical Integration contract; that does not grant Review Integration authority. The human message remains `READY FOR MERGE`.

This routing changes communication/state-transition behavior only. It does not expand Review authority, permit Review to implement fixes, or transfer Integration authority.

## Blocker fixes and recheck

If a fix is required, put the blocker and complete standalone fix-agent handoff on the PR: blocked PR/branch/head, failure mechanism, required change, scope/non-goals, invariants, expected evidence, and required new head SHA. The fix agent updates the existing PR unless Coordinator explicitly requires replacement. Record exactly one after-fix disposition: `FOCUSED RECHECK`, `MECHANICAL CHECK ONLY`, `NEW SPECIALIST REVIEW`, or `NORMAL MERGE REVIEW`.

After an isolated blocker fix, normally perform a focused recheck on the new exact head rather than a fresh broad review. Reopen broader review only when the fix materially changes the previously accepted design or exposes a new merge-critical uncertainty.
