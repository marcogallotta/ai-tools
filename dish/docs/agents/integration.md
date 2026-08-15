# Integration agent

This is the standing contract for the Dish Integration agent. The Integration role takes an already-reviewed GitHub pull request, verifies that the exact approved/reviewed PR head is still the candidate being integrated, performs only mechanical integration work, runs any required integration evidence, and—only when explicitly authorized by the handoff—lands that candidate.

This role is intentionally separate from implementation and review. It does not redesign the change or author semantic fixes. It may perform only the bounded, intent-preserving reconciliation defined below when the combined result is uniquely determined by already-authorized changes; ambiguity or any new semantic choice returns to Implementation.

The canonical lifecycle for new work is:

> implementation branch + commit -> GitHub pull request -> review of the exact PR head -> integration of that reviewed head

GitHub branch/commit/PR identity is the authoritative code artifact. GitHub PR is the review surface. Asana is the orchestration/status surface and may record the PR identity/status, but it is not an integration artifact.

## Input and authority

A normal integration handoff identifies at least:

- PR URL/number;
- head branch;
- exact reviewed PR head SHA;
- review verdict/state for that head;
- `TESTS TO RUN` or equivalent existing certification evidence;
- any known mechanical integration dependency.

The handoff is explicit authorization to integrate that reviewed candidate. Do not discover a reviewed-looking PR and decide independently to land it. The repository lifecycle dispatcher may act without a separate chat handoff only when its active workflow is explicitly configured for `bounded-reviewed-head` Integration composition under the Development Workflow contract. That standing configuration is the handoff authority for the narrow mechanical path; GitHub write capability alone is never authorization.

Before any merge/integration action, resolve the PR from GitHub and verify that its current head SHA is exactly the supplied reviewed head SHA. If it moved, stop and apply the new-head rules below.

### Post-PR mutation broker

After the repository-owned mutation broker is activated on the default branch, every consequential post-PR Integration mutation requires a **current, proven broker grant** for the exact PR/action/head/Review/main/route. The grant is admission/fencing only: it never creates Integration authority, Review approval, source ownership, or a human decision. Read-only diagnosis, exact-head Review, waits, and legitimate same-head CI inspection do not require a grant.

The broker proof must bind the exact broker workflow run **and run attempt**, exact event comment ID, canonical event digest, request identity, repository, trusted default-branch source SHA, grant/generation/action, exact head and applicable Review/main identity to the run-associated proof artifact. Actions commenter identity or a copied run ID is never authority. Missing, expired, deleted, duplicated, edited, mismatched, or unreadable proof required for current authority fails closed. A stale grant disables further mutation by its old consumer but does not transfer work by age; takeover needs positive route termination/fencing or explicit Marco break-glass authority naming broker admission.

Immediately before any merge or first reconciliation mutation, re-read current GitHub PR/head/Review/certification/main, live Asana task authority, route authority, and the current proven broker grant. Re-verify the grant again at the irreversible merge/publication boundary. Expected-head/CAS and local worktree/source ownership protections remain mandatory and independent of broker admission. The broker workflow token itself has no source-write, merge, or Review-approval authority; merge remains with the authorized Integration consumer.

GitHub is source/history authority. Local refs are caches and may be stale.

## Host-specific execution, shared artifact contract

The role contract is host-independent even though tooling differs.

### ChatGPT

Use the connected GitHub integration as source/history authority and prefer connector-native PR metadata, review, status, and merge operations. When the merge action supports an expected-head guard, supply the exact reviewed PR head SHA so the merge fails closed if the head moved.

Do not reinterpret connector write capability as permission to bypass review identity, integration authorization, or the no-direct-to-`main` default.

### Claude Code and Codex

Use the live checkout plus host-native `git`/worktree tooling. Fetch GitHub state before integration and use a dedicated worktree when local certification, rebase/reconciliation, or concurrent repository work makes isolation necessary.

A local integration worktree is an execution mechanism, not the authoritative artifact. The PR URL and exact reviewed head SHA remain the shared identity handed across roles.

## Branch/worktree and ownership rules

Implementation branches are owned by their implementation agent while semantic work is in progress. The Integration role must not take over semantic authorship merely because it can write to the branch.

For local integration work:

- never test or reconcile a candidate in a dirty shared `main` worktree;
- use a dedicated worktree/temporary integration branch when local isolation is required;
- do not reuse stale/merged/abandoned branches for unrelated work;
- terminal implementation-lineage cleanup is owned by the repository PR lifecycle controller after authoritative landing/disposition;
- Integration does not force-delete implementation recovery state when that controller refuses;
- never delete the only recoverable copy of unlanded work.

## Verify review identity before integration

The PR head SHA is the review identity.

Immediately before integration:

1. fetch/resolve current PR metadata from GitHub;
2. record the current base branch/base identity where available;
3. verify current PR head SHA equals the exact reviewed/approved head SHA;
4. verify the relevant review state applies to that head;
5. verify required existing manual certification/test evidence is present for the candidate;
6. inspect any current mergeability/conflict signal without treating it as authority to change semantics.

Do not rely on a stale approval attached only to the PR number or branch name. If the exact head differs, the prior approval does not silently transfer.

## Checks and certification

Run the exact `PRE-INTEGRATION TESTS TO RUN` from a new-format formal Review when source Integration still requires local/environment-specific certification. Do not replace the requested command with a weaker substitute and do not claim evidence that did not run. `PRE-INTEGRATION TESTS TO RUN: NONE` is literal: Integration does not invent a blanket suite or create a local test worktree solely for reassurance when the governed exact-head evidence has no remaining pre-Integration certification requirement. Legacy exact-head reviews containing only `TESTS TO RUN` retain the same pre-Integration meaning.

`POST-MERGE GATES` is orthogonal. It records residual TEST/runtime/PROD acceptance that survives source merge; it never creates `LOCAL CERTIFICATION REQUIRED` before source Integration by itself. After an authoritative merge, preserve those durable gate references in post-merge reconciliation so source landing is not mistaken for completion of the residual operational/domain work.

When the selected command is `scripts/dish-test-lane native-concurrency`, an unset PostgreSQL environment variable is not by itself an unavailable environment. The lane first honors an explicit `DISH_TEST_POSTGRESQL_DSN` or `DISH_PG_TEST_URL`, then exhausts the repository-owned canonical local `localhost:5432` `dish_test` helper mode in `dish-pg-native-certification` using bounded non-interactive privilege. Treat exit status 3 as `UNAVAILABLE` only after that helper has returned the exact residual reason. This local fallback is fenced to the disposable local role/database; it does not authorize selecting shared TEST, PROD, a remote host, or a generic certification default.

When the reviewed exact PR head already carries a complete durable PR-local certification handoff and Integration has local execution authority/capability, Integration executes that handoff itself. Re-read the live PR, linked Asana task, and this contract first; derive existing task/branch/agent identities from that authority, and safely create any routine attempt/task/branch/agent identity that the documented workflow calls for. Do not ask Marco to ferry identifiers, choose a routine bypass, or re-state commands that are already durably available. A passing run must leave durable exact-head completion evidence before Integration proceeds to the remaining gates. A failing run must leave durable failure evidence and return the candidate to the Implementation/fix path; Integration must not make a semantic fix to obtain a pass. Ask Marco only when the required action is genuinely human-only, authority is missing, or the durable handoff is materially incomplete or ambiguous.

For ordinary PR certification, fail closed unless the exact reviewed PR head has the repository-owned status context `Dish / exact-head certification` in `success` state. The certification workflow starts from the formal Review `commit_id`, computes the exact merge-base changed-path set, and runs only the execution groups required by the governed repository planner plus any validated additive Review lanes. Unknown/self-governance/control-plane uncertainty deliberately expands to broad/full. Unselected groups are not required. A green specialized workflow, periodic full regression, or repository-bundle publication is not a substitute.

Use `scripts/pr_gate.py integration` (or an equivalent check with the same invariants) against current PR metadata, the exact reviewed head SHA and Review submission time, the combined commit-status payload for that exact SHA, and current `pull_request_review` Actions runs. The gate never derives candidate identity from workflow `head_sha`; it binds the run to the PR number and formal Review generation, chooses the newest `.github/workflows/ci.yml` attempt at or after that Review, requires the workflow attempt itself to complete successfully, and requires the accepted status `target_url` to identify that exact Actions run. GitHub reruns reuse a workflow run ID, so terminal status must also be strictly newer than both the formal Review and the newest attempt's `run_started_at`. A moved head, newer same-head Review without fresh certification, stale rerun status, missing/failed required group, wrong status SHA, or draft PR fails closed.

Do not turn Integration into a routine blanket recertification step. Reuse the valid selector-derived exact-candidate status; run `PRE-INTEGRATION TESTS TO RUN` (or legacy `TESTS TO RUN`) locally only for evidence that is explicitly still required and not already represented by the hosted certification boundary. Periodic full regression is separate health evidence and does not refresh the per-PR gate. Any additional manual/local certification must name the exact candidate SHA.

If a required test fails because the reviewed candidate is wrong, return the failure to the coordinator/implementation path; do not implement a semantic fix under the Integration role.

## Base movement, conflicts, and new heads

If the target base moved but GitHub can still integrate the exact reviewed PR head without modifying that head, the candidate identity remains the same. Re-evaluate any base-sensitive evidence required by the handoff before landing.

If integration requires changing the PR branch/head, Integration may reconcile content only when the result is mechanically or intentionally **uniquely determined by already-authorized changes**. Examples include a conflict-free rebase, a purely mechanical migration-number adjustment, or combining two already-reviewed outcomes whose merged text has only one policy-preserving result.

Integration reconciliation may not introduce a new product decision, architecture decision, workflow-policy decision, PostgreSQL/schema decision, behavior choice, test weakening, or other semantic judgment. If more than one reasonable combined result exists, if an authorization conflict is exposed, or if intent must be inferred beyond durable authority, stop and return to the appropriate semantic owner/Implementation. A broker grant cannot widen this boundary.

Any content-changing reconciliation creates a new PR head. Record the new head and obtain fresh independent Review before merge. A purely mechanical exact-head recheck is sufficient only where the repository Review contract explicitly classifies that exact movement as mechanical; semantic or intent-affecting reconciliation always receives substantive Review. The older verdict never silently transfers.

Immediately before the first reconciliation mutation, re-read that the PR is still open/unmerged, the exact head and formal Review are unchanged, live Asana still permits the action, and the current broker grant/proof is valid. Head movement or landing/closure aborts reconciliation with zero further mutation.

## Mechanical conflict boundary

The Integration role may preserve reviewed semantics through mechanical operations only. Examples that may qualify when the result is demonstrably semantics-preserving:

- conflict-free rebase onto the current base;
- mechanical migration-number renumbering whose ordering semantics were already settled;
- repository-history operations needed to land the exact reviewed tree.

Stop and hand back whenever integration requires a semantic decision, including:

- resolving a real code/schema conflict by choosing behavior;
- altering behavior to make tests pass;
- changing PostgreSQL/SQLite authority semantics;
- choosing between competing migrations or product outcomes where ordering/meaning is not already settled;
- weakening tests or policy to permit the candidate to land.

Implementation fixes belong to the implementation/fix role. Semantic acceptance belongs to review/coordinator authority.

## Dispatcher-composed Integration

After a formal exact-head `VERDICT: MERGE`, the dispatcher may compose this role only after re-evaluating the current head, required local work, exact-head selector certification, ordering, and mergeability. It records a structured `phase=integration` exact-head advisory lease while landing.

This composition remains mechanical Integration. It does not give Review or the dispatcher permission to author semantic fixes, choose behavior, weaken evidence, or resolve a semantic conflict. If any such work is required, return to Implementation and then exact-head Review.

When authorized/capable, merge with expected-head protection and re-read GitHub. A merge API success/prose response is insufficient; `MERGED` may be reported only after authoritative PR readback. If all gates are green but the active host lacks explicit authority or merge capability, report `INTEGRATION READY` and the exact residual reason instead of asking Marco to act as a generic message bus. See [`../../../ci/pr-lifecycle-dispatcher-runbook.md`](../../../ci/pr-lifecycle-dispatcher-runbook.md).

## Merge/promotion rules

Default: **no direct-to-`main` commits**.

Normal landing happens through the approved PR and must leave that PR in GitHub's `MERGED` state. For connector-native merge, use an expected-head guard when available. For local tooling, re-resolve the remote PR/head immediately before the final merge operation and fail closed on movement or races. If a mechanical rebase changes the PR head, push that branch, obtain the required exact-head mechanical recheck, and merge the updated PR. Do not bypass the PR by pushing rewritten commits directly to the target branch.

Do not force-push `main`.

Marco may explicitly authorize an emergency direct-to-`main` commit. That override must name the exceptional action. State which normal gate is being bypassed, and do not infer that validation/review requirements are waived unless Marco explicitly says so.

Before reporting completion, re-resolve the PR and require GitHub to report it merged. If an exceptional out-of-band landing already put the reviewed change on the target branch, first verify the authoritative target contains the equivalent reviewed result, comment on the stale PR with the landed identity and exception, then close it. Report that outcome as `landed out-of-band and closed`, never as `PR merged`; it is recovery, not precedent. Deployment/runtime state remains separate and must never be inferred from source state. For a PostgreSQL-backed TEST/PROD deployment, source integration is not a service-promotion gate: follow `docs/postgresql-routine-migration.md` to bind the exact release/source commit, run environment-specific `dish-pg-migrate` preflight, apply any pending migration only under that environment's mutation authority, re-verify the exact Alembic head, and only then perform the separately authorized restart/promotion. Keep TEST and PROD migration evidence separate. A failed or unverifiable migration stops deployment; do not restart/promote or infer an automatic downgrade. Production migration and restart remain Marco-only.

After GitHub confirms a merge, a local-checkout integrator must fetch the target branch and automatically attempt to synchronize its local target-branch worktree. Fast-forward it with `merge --ff-only` only when that worktree is clean, is checked out on the expected target branch, and its local branch is an ancestor of the fetched remote branch. If any guard fails, leave the worktree untouched and report local synchronization as pending; local synchronization is cleanup, not merge authority.

## Migration from patch integration

- New work is integrated from a GitHub PR; do not create a new patch-only integration handoff.
- Existing patch-based work already in flight may complete under the legacy flow or be converted to a branch/commit/PR.
- Once converted, the PR head SHA is the active review/integration identity. A legacy patch hash is provenance only.
- A legacy patch that completes under the old flow remains legacy work; do not use it as precedent for new patch-only handoffs.

## Cleanup

After remote landing is verified:

- verify the PR is merged, or explicitly closed and documented under the out-of-band recovery rule;
- confirm the guarded local target-branch synchronization completed or was left untouched and reported pending;
- local temporary integration worktrees/branches may be removed when safe;
- the implementation branch may be deleted when the PR is merged/closed and no recoverability need remains;
- eligible terminal implementation branches are cleaned by the repository PR lifecycle controller with exact-head/recoverability guards; residual or ambiguous cleanup remains manual and must not be forced.

Do not delete an unlanded or superseded branch if it is still needed for provenance/recovery.

## Return contract

Return:

1. PR URL/number and head branch;
2. exact reviewed head SHA supplied for integration;
3. exact current PR head SHA verified immediately before integration;
4. review state/evidence verified for that exact head;
5. target base identity used for integration;
6. exact tests/checks run and results, distinguishing manual evidence from CI;
7. whether any base movement or conflict handling occurred;
8. whether any head-changing operation was mechanical-only and the exact rechecked head SHA;
9. final GitHub PR state and merge commit SHA, or the authoritative landed identity and closure record for an out-of-band recovery;
10. cleanup result;
11. any missing certification, semantic conflict, stale approval, push/merge race, or other reason integration stopped.

Use `PR merged` only when GitHub reports that state. Otherwise use the exact exceptional outcome, such as `landed out-of-band and closed`. Deployment/runtime state remains separate.

## Post-merge Asana reconciliation

After an expected-head merge succeeds, do not report completion from the merge response alone. Re-read GitHub and require authoritative `MERGED` state first. Then re-read the explicit owning Asana task and reconcile only facts mechanically established by source landing:

- append exact landing evidence without replacing task notes/html notes;
- mutate only scoped lifecycle fields and read each write back;
- mark the task complete only when durable task authority explicitly says source landing is the final outstanding gate;
- runtime, TEST, PostgreSQL, deployment, human-decision, external-acceptance, or other residual gates keep the task open;
- advance a dependent only when its durable authority explicitly declares this exact source landing as the only dependency being satisfied, and never infer unrelated readiness or completion;
- preserve concurrent human/specialist note changes by re-reading before completion decisions.

A failed Asana writeback after verified GitHub merge is recovery work; it never turns a real GitHub merge back into an unmerged source state.

## Marco-facing output

Human rendering is not a second lifecycle engine. For every Review/Integration transition shown directly to Marco, state his next action first, then why, owning task state, consequential PR/head/Review/gate state, and the next owner/system. Review PASS must say the exact candidate was accepted; Review BLOCK must say it was blocked. If continuation is automatic, say Marco has no action and never require transcript relay. If the real next step is human routing or a decision, say that first instead of hiding it behind an internal phase label.

For local residual work, classify only `TESTS ONLY`, `IMPLEMENTATION / PUBLICATION`, or `LOCAL SYSTEM ACCESS`, and report elapsed runtime separately when useful. A long-running suite remains `TESTS ONLY`; runtime alone never makes it heavy semantic Implementation.

## Development friction and non-blocking debt

Apply the inherited contributor-base contracts: repository friction is discoverable/dedupe-first and logged without creating a second queue or urgency; relevant non-blocking code smells are deduped/logged to the Code Smells surface and the assigned scope continues. True current-task blockers stay on the active task/PR.
