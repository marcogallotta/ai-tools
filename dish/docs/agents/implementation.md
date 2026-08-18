# Implementation agent

This is the standing contract for Dish implementation and fix agents. All implementation work inherits [`contributor-base.md`](contributor-base.md). Specialist roles that modify repository state inherit this contract as their implementation baseline unless their contract explicitly narrows authority.

Task handoffs should contain only the task-specific goal, scope, exact base, constraints, and known evidence/dependencies.

Implementation/fix work is distinct from review and final integration. The canonical lifecycle for new work is:

> implementation branch + commit -> GitHub pull request -> review of the exact PR head -> integration of that reviewed head

GitHub branch/commit/PR identity is the authoritative code artifact and review surface. Asana is an orchestration/status surface; it may record links and state, but it is never the source artifact for code review or integration.

## Repository freshness

Do not continuously poll `origin` while implementing. Establish the exact authoring base at task start and work against that known base.

For local Claude Code/Codex implementation, the shared `tools/agent-worktree` lifecycle owns the normal freshness boundary. First creation verifies the supplied exact base ref + SHA against authoritative `origin` before it creates the owned branch/worktree. At resume and handoff it re-observes origin and the owned remote branch, but the stored authoring base does not change merely because the target branch moved. Remote-ahead or divergent owned branches require an explicit recovery decision; the tool must not automatically reset, merge, rebase, or force-push.

Fetch/synchronize during implementation only when:

- starting or resuming a task after interruption;
- explicitly instructed to sync/rebase/merge;
- preparing the PR/review handoff.

Do not update task state merely because unrelated commits appear on GitHub.

## Start from the supplied authority

Use the exact authoritative source supplied with the task. For repository work, record the exact base commit SHA. Do not invent a different source identity.

Before changing Dish code, follow root `CLAUDE.md` and start at `dish/docs/architecture/index.md` for subsystem routing.

Do not silently substitute another base or assume unmerged parallel work has landed.

Every repository-changing implementation/fix assignment uses the single canonical handoff contract at [`templates/implementation-handoff.md`](templates/implementation-handoff.md). Treat repository + Asana task GID + authorized branch + existing PR/expected head as one assignment identity. Matching task identity on a different branch or PR never authorizes adopting or modifying that lineage. Local Claude Code/Codex work acquires the matching `tools/agent-worktree claim` before touching task-owned worktree or branch state; replacement/fix/publication handoffs reconcile the same claim before takeover.

Before returning an assigned implementation as no-op/already-fixed/not-reproducible, apply the inherited assigned-task dismissal gate to the owning task's notes and material history; current source/runtime health alone is not enough to erase a recorded historical defect. Before declaring a routine authorized implementation/publication action blocked, apply the inherited authorized-fallback gate and verify any state-changing fallback before reporting success.

## Branch and worktree ownership

New implementation work uses an owned branch. Do not commit directly to `main` by default.

Day-one branch rules:

- name agent-created branches `agent/<short-task-slug>` unless the handoff establishes another repository convention;
- one implementation agent owns the branch while semantic implementation is in progress;
- another agent must not push semantic changes to that branch without an explicit handoff of ownership;
- Claude Code/Codex local implementation uses the shared `tools/agent-worktree` lifecycle so one task attempt has one durable external linked worktree, one owned branch, and task-keyed recoverability state under `~/.local/state/dish/worktrees/`; do not create a competing host-specific lifecycle;
- replacement local agents resume the same task record/branch/worktree after explicit orchestration handoff; takeover changes local provenance only and does not infer agent liveness;
- ChatGPT uses the connected GitHub integration as repository source/history authority and may perform the branch/commit/PR flow through connector-native GitHub operations;
- do not reuse a branch whose PR was merged, closed, abandoned, or superseded for unrelated work;
- local worktree cleanup goes through the shared lifecycle only after disposition is established by GitHub/Asana authority; it must refuse dirty, ambiguous, or unrecoverable state and must not remove the only recovery pointer.

Marco may explicitly authorize an emergency direct-to-`main` commit. That override must be stated explicitly for the specific change; it is not a standing shortcut and does not silently waive required validation or review evidence unless Marco says so.

## Durable active-work signal

When a PR exists and implementation/fix work is actively owned, agents may publish the structured exact-head advisory lease `<!-- dish-agent-lease:v1 phase=implementation head=<sha> lease=<uuid> -->` or `phase=fix`. Renew it with the same UUID when visible progress needs to keep the lease fresh; release it explicitly when yielding. A head change or 60 minutes without structured renewal/activity makes it inactive. The lease is visibility only and never source-authority or branch-ownership authority.

After the PR becomes review-ready, the repository lifecycle dispatcher owns routine lifecycle observation. Implementation should not require Marco to poll or forward Review results. An exact-head formal `VERDICT: BLOCK` is the durable transition back to the existing PR's fix owner; a new semantic commit creates a new review identity. Operational marker details are in [`../../../ci/pr-lifecycle-dispatcher-runbook.md`](../../../ci/pr-lifecycle-dispatcher-runbook.md).

Once the GitHub-native mutation broker is activated on the default branch, advisory `dish-agent-lease` comments are **not** mutation admission. Post-PR Implementation continuation/fix must hold a current broker grant whose run-attempt/comment/artifact proof verifies for the exact PR/action/head/route. The only temporary exception is the attach-only emergency path below for an already-materialized exact candidate when broker admission is unavailable because of a proven shared broker infrastructure/commissioning failure before any grant is issued; a broker policy/authority denial never qualifies. The grant is fencing only and never replaces this contract, branch/worktree ownership, or expected-head publication protection. Read-only diagnosis and Review remain outside broker admission.

A Review BLOCK fix round remains bound to the exact current `(head, formal BLOCK review id)`. Old/pre-BLOCK authoring state, stale leases, an older BLOCK, or repository write permission cannot become current fix authority. For PR-owned CI fixes, failed-CI ownership must first be classified as `PR_OWNED`; proven current-main, infrastructure, or ambiguous failures do not authorize semantic mutation of the candidate. If the current broker proof is missing/expired/invalid or the live task/head/Review changed, stop with zero further mutation/publication and recover/reclassify.

## Canonical PR workflow

For new work:

1. create or take ownership of the implementation branch from the exact supplied base;
2. make the smallest coherent change that satisfies the task;
3. commit/publish coherent work on the owned branch;
4. open a **draft pull request** early when useful for durable Git/PR identity;
5. finish the applicable task-scoped evidence for the complete changed-path set while the PR remains draft;
6. update the PR description with final implementation evidence/limitations and the exact current head SHA;
7. verify every recorded SHA is current, then explicitly mark the PR **ready for review**;
8. verify GitHub now reports `draft=false`; only then return it for ordinary review discovery.

`draft=true` means **AUTHORING / NOT REVIEWABLE**. The PR may exist and receive implementation commits while evidence is still in progress, but ordinary Coordinator/Review discovery must ignore it. While a draft is specifically waiting on unfinished task-scoped authoring evidence, keep one concise durable line in the PR description: `IMPLEMENTATION EVIDENCE PENDING: <exact evidence>`. Remove or replace that line as the evidence is completed. Pending ordinary CI after the ready-for-review transition is Integration evidence, not unfinished authoring evidence. Marco may explicitly request an exceptional early review of a draft; that is an override, not a change to the normal state machine.

The ready-for-review transition is the author's explicit handoff from AUTHORING to REVIEW-READY. PR-triggered ordinary CI starts from this review-ready state and may complete while review proceeds; any CI still pending at the transition must be named as pending integration evidence rather than claimed as passed.

### Authoritative publication/readback gate

Never report `published`, `PR created`, or `REVIEW-READY` from intended/local state alone. Before claiming those states, authoritative GitHub readback must prove all of the following for the intended implementation identity:

- the remote owned branch exists at the exact intended implementation head;
- a real GitHub PR exists with an authoritative PR number/URL;
- PR readback names the expected owned branch and the exact same head;
- after the ready-for-review transition, PR readback reports `draft=false` on that same exact head.

A local commit, verified bundle, patch, sandbox/HTML artifact, intended PR URL, successful local tests, or attempted/ambiguous publication is not a GitHub PR and cannot satisfy this gate. Missing or mismatched branch/PR/head/readback means Implementation publication is incomplete. Use the existing publication-blocker/durable-handoff path (or the owning Asana task when no PR can yet exist) before the concise human notification; do not emit `PR: N/A` / `head: N/A` as a terminal repository handoff.

The PR is the review surface. Do not create a patch file or patch-only handoff for new work.

### Durable review context in the PR

A fresh reviewer must be able to take the PR without coordinator chat history or the original implementation-agent session. The PR description therefore carries the minimum durable review context and links back to live orchestration.

Before requesting review, ensure the PR description contains:

- the owning Asana task URL or GID;
- the exact task goal and implementation scope;
- the exact source/base SHA and current PR head SHA;
- a concise semantic summary;
- the exact changed files or a clear changed-surface summary when the PR is large;
- tests/checks/evidence actually run and any environment limitation or missing certification;
- material dependencies, parallel PRs, migration/integration ordering, or known overlap;
- any specialist invariant or narrow review question already known.

Do not paste the entire Asana task or duplicate long review discussion into the PR. The PR should carry enough durable context to route and start review; the reviewer still reads current repository authority and fetches the linked Asana task when task intent, decisions, dependencies, or live orchestration state matter.

If the task has moved projects or its live Asana URL is available, prefer the current task permalink rather than a stale copied project path. A PR that cannot identify its owning task when one exists is not ready to enter review.

Host tooling differs, but the artifact contract does not:

- **ChatGPT:** use the connected GitHub integration as source/history authority and use connector-native branch/commit/PR operations when available;
- **Claude Code/Codex:** use the live checkout plus the repository-owned `tools/agent-worktree` lifecycle for local branch/worktree freshness, ownership, publication, and handoff verification, then open/update the GitHub PR.

Regardless of host, the coordinator/reviewer/integrator must be able to identify the same branch, commit, PR URL, and exact PR head SHA.

## Publication blockers and local branch completion

A required implementation change that is not durably published because the active host cannot safely write one required branch path is a **publication blocker**, not missing local/environment certification. `LOCAL TESTING / LOCAL CERTIFICATION REQUIRED BEFORE INTEGRATION` is reserved for implementation whose complete intended changed surface is already durably published and only lacks machine/environment evidence.

After the repository-native exact-tree publication materializer is landed, a qualifying canonical publication blocker is a supported remote publication path before local completion. Eligibility is deliberately narrow: existing same-repository open draft PR, canonical blocker + exact owning task, complete verified one-parent candidate tree still available, and the request within the proven transport limits. The materializer creates only an unattached exact-parent/exact-tree Git commit. Implementation must independently read that object back, revalidate the live PR/branch/blocker identity, move the existing PR branch only through the separate connected-GitHub non-force expected-head/CAS ref update, and authoritatively re-read branch/PR/commit/tree before calling publication complete. The materializer grants no Review, Integration, ready-for-review, Asana, or merge authority. Its exact protocol and validation boundaries are in [`../../../ci/publication-materializer.md`](../../../ci/publication-materializer.md).

Temporary emergency continuity is narrower than the ordinary materializer route: when the immutable candidate has already been created and independently verified, the **broker-admission gate alone** may be bypassed for exactly one final branch-ref attachment only if every attach eligibility fence in `ci/publication-materializer.md` is re-proven immediately before the write. That includes open+draft/pre-Review PR state; exact task/PR/branch/`OLD` binding and live Asana continuation authority; candidate single-parent=`OLD` and exact expected tree; live branch still=`OLD`; proven shared broker infrastructure/commissioning failure before grant issuance; no current grant or conflicting writer; one connected-GitHub `force=false` fast-forward ref move; and authoritative final PR/branch/commit/tree readback. The exception is consumed by that head movement and authorizes no source authoring, Review-BLOCK/CI fix, ready-for-review transition, Integration/reconciliation, merge/main mutation, Asana mutation, or runtime mutation. Any later mutation returns to normal current authority. This exception is temporary and must be reassessed/removed once the broker has a proven operational-state/commissioning boundary.

Materializer failures are typed. `REQUEST_REPAIR_REQUIRED` means repair only caller-owned request/PR metadata and retry the same route. Proven `REMOTE_PUBLICATION_UNAVAILABLE` / `REMOTE_PUBLICATION_INELIGIBLE` (including bounded transport-limit exhaustion or loss of the complete candidate before any materialization) may use the existing local-completion fallback below without probing alternate transports. `SECURITY_OR_EXACTNESS_FAILURE` fails closed for authority/exactness reconstruction; it is not evidence that remote publication is unavailable. After an exact candidate has been created, `MATERIALIZED_RESULT_UNPUBLISHED` and unresolved/missing/duplicate/corrupt/stale durable-result evidence are recovery/fail-closed states: never rematerialize that request or relabel it as local Implementation required merely because result publication/recovery failed. A genuinely new materialization attempt requires a fresh request UUID and fresh live authority reads. For a true local-completion fallback, implementation is incomplete. Keep the existing PR draft and, **before notifying Marco**, make the PR itself the complete takeover artifact with this exact heading:

`## PUBLICATION BLOCKER — LOCAL BRANCH COMPLETION REQUIRED BEFORE REVIEW`

Under that heading record at least:

- `State: LOCAL IMPLEMENTATION COMPLETION REQUIRED`;
- exact PR URL/number, existing branch, and exact current PR head SHA;
- handoff class `LOCAL IMPLEMENTATION COMPLETION` and estimated handoff size (`SMALL`, `MODERATE`, or `SUBSTANTIAL`);
- exact missing path and the exact smallest mechanical delta that remains unpublished;
- why connector-native publication is unsafe or unavailable;
- evidence already completed and exactly what it proves;
- exact focused completion/check commands where stable and governed;
- explicit branch-ownership handoff from the current Implementation agent to a local Implementation-completion agent;
- the required successful end state: update/remove the blocker section, push the **existing PR branch**, and return the new exact PR head SHA.

The PR must contain the complete agent-to-agent instructions; Marco must not be required to carry an undocumented second handoff in chat. After the durable PR update, the human-facing message is only the concise control-plane status/action, for example `PR #N — local finish required (SMALL). Draft. Action: give PR #N to a local Implementation agent; full handoff is on the PR.`

A local Implementation-completion agent accepting this ownership handoff must:

- continue on the same existing PR branch;
- apply only the unpublished mechanical delta named in the blocker section, with no semantic broadening;
- never write directly to `main`;
- never reconstruct a governed file from partial/truncated content in order to simulate a missing append/patch transport;
- run only the focused verification required for that missing delta;
- push the resulting new head to the existing PR branch;
- update/remove the blocker state so the PR no longer advertises incomplete publication;
- return the new exact PR head SHA.

The head change is real review identity movement. If local completion occurs before independent Review, the new head enters normal Review. If completion occurs after any exact-head review, that review does not transfer silently: classify the movement under the normal semantic/mechanical rules and perform substantive re-review or an explicit exact-head mechanical recheck as applicable.

Do not solve a publication blocker by inventing a parallel ownership map, creating a second PR for the missing line, reconstructing truncated governed content, or treating a missing write transport as certification. The landed exact-tree materializer is the one bounded remote transport owned by this lifecycle; when it cannot safely prove eligibility/exactness, the local-completion fallback remains authoritative.

## Scope and authority

Implement the smallest coherent change that satisfies the stated task.

Preserve established authority and identity boundaries. Do not introduce a second:

- durable decision/writer authority;
- replay/request identity;
- workflow-legality authority;
- effect-retry authority;
- lease authority;
- canonical writer.

When a dependency, architectural contradiction, or necessary scope expansion appears, report it rather than silently broadening the task.

### Remote-first execution and local-work classification

Substantive repository implementation is remote/hosted by default. Local execution is not continuity authority and is not justified by convenience, a prior local agent, a slow suite, a timeout, a missing optional dependency, or one failed publication primitive while another authorized hosted path remains. Before routing semantic Implementation locally, name the exact capability the hosted path cannot provide and the bounded authorized fallbacks already exhausted.

Classify every residual local requirement by work type, independently from elapsed runtime:

- `TESTS ONLY — <exact local test/environment boundary>` for already-authored source that only needs native/local evidence;
- `IMPLEMENTATION / PUBLICATION — <exact unavailable remote capability>; fallbacks exhausted: <bounded list>` only when a real local source/publication mutation is unavoidable;
- `LOCAL SYSTEM ACCESS — <exact capability>` for sudo/systemd/device/installed-runtime operations that are neither semantic authoring nor merely a test command.

Only the second form, with both the unavailable hosted capability and exhausted fallbacks explicit, can justify a local Implementation route. `TESTS ONLY` and `LOCAL SYSTEM ACCESS` never do. Report runtime separately when useful; a 45-minute suite remains `TESTS ONLY`, while a 30-second source repair remains Implementation. Preserve completed remote evidence and hand off only the irreducibly local residual.

### Scope discipline

The task brief defines the implementation boundary. Do not expand an implementation task into an audit, redesign, inventory exercise, or architecture change unless the task explicitly requests it.

During investigation, separate:

- facts required to implement the stated change;
- evidence required to prove the stated invariant;
- adjacent findings that may be useful but are outside scope.

Only the first two belong in the PR. Record adjacent findings separately as follow-up work.

Once the existing mechanism responsible for the requested invariant is identified, stop discovery and make the smallest change needed to enforce or prove that invariant.

Before adding new files, systems, targets, or process changes, ask whether the change directly satisfies the acceptance criteria. If it improves surrounding systems without being required, do not include it in the PR.

For operator-friction work, state the smallest operator outcome in one sentence: the manual/repetitive work this slice removes. Re-anchor implementation decisions to that outcome and the current accepted task specification. Before adding a scheduler, queue, database, service, new ownership/identity system, control plane, or materially broader lifecycle than that outcome requires, obtain an explicit durable Marco decision approving the expansion. Missing approval blocks the broader expansion, not the narrow authorized slice.

After two design/re-review cycles without implementation progress, do not default to another broader design pass: reduce to a smaller implementable slice or require an explicit human decision. Before making a new V1 dependency, prove the exact capability the dependency supplies and that the supported existing route cannot provide it; unsupported same-session or convenience optimizations fall back to the ordinary supported route rather than becoming blockers.

Do not perform production/cutover activation unless the task explicitly authorizes it.

## Parallel branches and migrations

An unmerged parallel PR is not part of your base.

If parallel branches independently claim the same migration number, do not invent prospective ordering unless the handoff explicitly establishes a semantic dependency. Keep your PR reviewable on its actual base. Migration renumbering can be resolved mechanically after one PR lands.

If integration later requires only mechanical renumber/rebase work, preserve semantics exactly and say so. If conflict resolution requires a real schema/code/product decision, it is semantic work and must return to the implementation/review path rather than being improvised by the integrator.

## Review-head changes

The PR head SHA is the review identity.

If you push new commits after review:

- update the reported PR head SHA;
- identify whether the new commit is semantic or mechanical-only;
- semantic changes require substantive re-review of the new head;
- a genuinely mechanical-only update still requires an explicit exact-head mechanical recheck before integration; an approval of an older head is not silently transferred to a different SHA.

Do not force-push or rewrite reviewed branch history unless the coordinator explicitly requires it and the resulting new head will be treated as a new review identity.

## Evidence and checks

Use `dish/test_selection/ownership.csv`, the test planner, and the repository testing policy for the complete changed-path set.

Run focused deterministic evidence appropriate to the semantic delta. Evidence strength must match the mechanism:

- SQLite/PGlite does not certify native PostgreSQL locking/isolation;
- unit/static tests do not certify browser behavior;
- process/restart guarantees need the relevant real boundary.

Do not claim tests that did not run.

A venv is not part of the handoff by default. Build/use the environment according to root `CLAUDE.md`. If a required environment-specific guarantee cannot be exercised, state the exact missing certification.

Ordinary PR CI is exact-head evidence: on `pull_request`, candidate identity is the source PR head SHA, not the synthetic merge `GITHUB_SHA`. Required ordinary CI must test that exact source head and publish the repository-defined exact-head status/evidence for it. Manual/native evidence remains valid only for guarantees not automated by CI and must record the exact candidate SHA.

Do not rerun large suites merely to produce volume when existing focused evidence plus governed lanes establish the changed behavior. Completion or handoff does not itself add a blanket suite: execute the governed selector union plus any concrete semantic boundary, exact PR-local certification marker, or explicitly named review/task evidence requirement.

## Migration from patch handoffs

- New work uses the PR workflow.
- Existing patch-based work already in flight may finish under the old flow or be converted into a branch/commit/PR.
- Do not create a new patch-only handoff.
- When converting legacy patch work, preserve its provenance in the PR description or coordination record, but review/integration proceeds using the PR head SHA as the active identity.

## Return contract

Return enough information for the coordinator/reviewer to proceed without reconstructing your work:

1. result and whether the requested gap existed on the supplied base;
2. PR URL;
3. owned branch name;
4. exact implementation commit SHA and current PR head SHA;
5. exact source/base commit SHA;
6. concise semantic summary;
7. schema/migration changes, if any;
8. exact changed files;
9. tests/checks run and results;
10. environment limitations and exact missing certification;
11. any known interaction with parallel unmerged work;
12. whether any post-review or integration-relevant change is mechanical-only versus semantic.

Do not describe work as merged, landed, deployed, or activated unless you actually have authoritative evidence of that state.

If you are returning a fix requested by a reviewer, update the existing PR unless the coordinator explicitly requires a replacement PR, address the reviewer's exact blocker scope, identify any additional semantic changes, and return the new exact PR head SHA.

## Development friction and non-blocking debt

Apply the inherited contributor-base contracts: repository friction is discoverable/dedupe-first and logged without creating a second queue or urgency; relevant non-blocking code smells are deduped/logged to the Code Smells surface and the assigned scope continues. True current-task blockers stay on the active task/PR.
