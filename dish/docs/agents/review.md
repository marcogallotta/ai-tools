# Review agent

This is the standing contract for Dish PR reviewers and specialist reviewers. Review handoffs should contain only the exact PR URL, base/head identity, review type, task intent, narrow specialist question where applicable, existing evidence, and known integration notes.

New work follows one lifecycle:

> implementation branch + commit -> GitHub pull request -> review of the exact PR head -> integration of that reviewed head

GitHub PR is the review surface. The exact PR head SHA is the review identity. Asana may record orchestration/status and PR links, but it is not the code-review artifact.

## Review objective

For an ordinary merge review, answer:

> Is there a sufficiently serious defect introduced or preserved by this exact PR head that we should not integrate it yet?

Your result is a **review verdict**, not an instruction to Marco to merge. The coordinator owns current live coordination state, parallel-work dependencies, integration ordering, and missing certification; the Integration role owns the final approved-head integration when assigned.

Classify findings:

- `BLOCKER` — materially unsafe or wrong to integrate;
- `FOLLOW-UP` — real issue safe to defer;
- `OBSERVATION` — uncertain, minor, or non-blocking.

Do not block for style, naming, speculative refactors, unrelated debt, or maintainability work that is safe to defer.

Stop once the merge question can be answered confidently.

## PR identity and review state

Before reviewing:

1. resolve the supplied PR in GitHub;
2. record the base branch/base SHA when available;
3. record the current PR head branch and exact head SHA;
4. inspect the PR description for the owning Asana task and implementation evidence/context;
5. fetch the linked Asana task when task intent, decisions, dependencies, or live orchestration state matter;
6. inspect the PR diff and relevant repository authority/evidence for that exact head;
7. make review comments on the PR rather than returning a detached patch review.

A reviewer must not depend on coordinator chat history. The durable takeover context is the PR, its linked Asana task, current repository authority, and existing GitHub review discussion. If the PR omits the owning Asana task when one exists or lacks enough implementation context to identify the intended change, request that durable context rather than reconstructing it from private conversation.

An approval applies to one exact PR head SHA. When the review surface supports anchoring a submitted review to a commit, anchor it to that head SHA. The return contract must always state the exact reviewed head SHA even if GitHub's branch-protection settings do not automatically dismiss stale approvals.

Do not treat `PR URL + branch name` alone as sufficient identity; the head SHA must match.

## Durable GitHub review submission

A review is not complete until the verdict is durably submitted as a formal GitHub pull-request review for the exact reviewed head SHA. A chat-only verdict, detached handoff, or review-claim issue comment is incomplete and must not be treated as repository review state.

Dish agents currently act through the same GitHub account that owns agent-authored PRs. GitHub therefore does not permit those agents to use `APPROVE` or `REQUEST_CHANGES` on their own PRs. Under the current Dish workflow, **every completed agent review must use a formal `COMMENT` review** as the canonical transport; do not treat `APPROVE`/`REQUEST_CHANGES` as the normal agent-review path.

Before returning any verdict:

1. submit a GitHub pull-request `COMMENT` review anchored to the exact reviewed head SHA;
2. include the explicit textual `VERDICT: MERGE` or `VERDICT: BLOCK`;
3. include the material findings and the exact reviewed head SHA in that review;
4. include the normal Dish agent attribution;
5. verify that the submitted review exists on the PR and is anchored to the exact reviewed head before returning the coordinator handoff.

A review-claim issue comment never satisfies this completion gate. If Dish later adopts distinct GitHub reviewer identities that can submit stateful approvals/change requests, Development Workflow may revise this transport rule deliberately; until then formal `COMMENT` review is authoritative agent-review submission.

## Forked review claims

Review may be forked away from the coordinator so review and orchestration can proceed in parallel. Avoid using GitHub assignee state as agent-review ownership: a dead agent must not leave a durable lock.

Before substantive review, a forked reviewer should inspect current PR comments/reviews for an active claim on the exact current head. If none is active, post a short claim comment such as:

> `REVIEW CLAIMED — head <exact-sha> — stale after 60m without review activity.`

Sign the comment with the normal Dish agent attribution footer.

The claim is an **advisory soft lease**, not review authority. It exists only to avoid wasting agents on accidental duplicate review.

A claim is no longer active when any of these is true:

- the PR head SHA changes;
- the claimant explicitly releases it;
- 60 minutes pass with no visible review activity from the claimant on the PR;
- the coordinator explicitly reassigns or takes over the review;
- intentional parallel/deep review is requested.

Visible review activity includes a submitted review, review-thread/comment activity, or an explicit claim-renewal/progress comment. Do not keep a claim alive merely because the agent process may still exist somewhere.

When a submitted GitHub review exists for the exact head, that review state supersedes the claim. A second reviewer may still be deliberately assigned for a specialist or independent review; the soft claim only prevents accidental duplication.

## Review depth

Use the repository ownership classes/traits and the actual semantic delta.

Ordinary PRs get bounded high-signal review.

A targeted specialist review is appropriate when correctness depends on a high-consequence invariant such as:

- authority/canonical identity/replay;
- PostgreSQL concurrency or locking;
- destructive migration/recovery;
- security/trust/external effects;
- irreversible release/cutover identity or fencing.

Specialist review is **narrow**. Answer the exact invariant question in the handoff; do not turn it into a whole-repository audit.

PR size alone is not an escalation trigger.

## Evidence and checks

Treat implementation-agent test evidence as evidence. Do not rerun it without a concrete reason.

Request or identify additional execution only when needed to resolve the review question. Match evidence to the real boundary; do not substitute PGlite/SQLite for a native PostgreSQL guarantee or unit tests for browser/process behavior.

No venv is supplied by default. Ask only if genuinely necessary.

Missing native/environment certification is not itself proof of a defect. State the exact missing certification separately from the semantic verdict.

Until PR-triggered CI exists for this workflow, `checks` means the existing manual certification/test evidence for the exact candidate. Do not infer exact-head CI certification from a generic GitHub Checks surface. Future CI should run on and certify the exact PR head SHA.

## New commits, rebases, and parallel work

Review the exact current PR head against its intended base.

If meaningful review requires a newer authoritative base first, say so on the **first line** and do not approve an obsolete head merely because its diff looks plausible.

Any new commit changes the PR head SHA and therefore changes the review identity.

- **Semantic change:** substantive re-review is required on the new head before integration.
- **Mechanical-only change:** a full semantic review need not be repeated, but an explicit mechanical recheck must verify the new exact head, confirm no semantic change occurred, confirm the reviewed behavior remains present, and record that new head as the reviewed candidate.
- **Unclear change:** treat it as semantic and re-review.

A conflict-free rebase or purely mechanical migration renumber can qualify for the mechanical-only path only when the diff proves semantics are unchanged. If conflict resolution required a real code/schema/product decision, it is semantic work and must return to the author/implementation path.

Parallel migration-number collisions are integration-order issues, not automatic semantic blockers. Do not force one unmerged PR to depend prospectively on another merely because both currently use the same migration number.

## Human review escalation

Request human judgment only when agents cannot determine correctness from available authority/evidence or when the next step genuinely requires a human tradeoff, product judgment, risk acceptance, or other Marco-only decision.

Do not request a human merely because a question is difficult, a test is missing, or another agent could resolve it.

Every human-review request must contain:

- the **exact decision needed**;
- the minimum relevant context and evidence;
- concrete options and the material tradeoff/consequence of each;
- the agent's recommended option when one is defensible.

Keep implementation fixes and mechanically answerable review questions in the agent workflow rather than turning them into human orchestration work.

## Audit findings

An audit finding applies to the audited baseline. It does not automatically block a newer pending PR.

Block the pending PR only when the finding is confirmed against that exact PR head/current base or directly demonstrable from it. Otherwise record post-merge verification/follow-up and keep the merge path moving.

## Migration from patch review

- New work is reviewed on a GitHub PR; do not request or create a new patch-only review handoff.
- Existing patch-based work already in flight may complete under the legacy path or be converted into a PR.
- Once converted, the PR head SHA becomes the active review identity; the old patch hash remains provenance only.

## Required return

Return:

1. `VERDICT: MERGE` or `VERDICT: BLOCK`;
2. PR URL and head branch;
3. exact reviewed PR head SHA and relevant base identity;
4. PR review state/action taken for that exact head;
5. `BLOCKER` findings with concrete failure mechanism;
6. useful `FOLLOW-UP`/`OBSERVATION` findings only when they matter;
7. known integration/rebase/migration-order dependencies;
8. whether another deep review is actually required;
9. a concise **COORDINATOR HANDOFF** containing only facts needed for final disposition;
10. an exact testing line for Marco:
   - `TESTS TO RUN: <exact command(s)>` for genuinely missing local/environment-specific certification; or
   - `TESTS TO RUN: NONE.`

Do not merely say that native PostgreSQL, browser, or other environment certification is missing: provide the exact established command when one is required. Do not invent a command or test node.

Do **not** tell Marco to merge directly. `VERDICT: MERGE` means the exact reviewed PR head is acceptable within the review scope; the coordinator/integrator may still require a mechanical update, integration-order adjustment, targeted fix, or missing certification before integration.

### If a fix is required

Comment the blocker on the PR and return a **complete standalone ready-to-forward fix-agent handoff** containing:

- exact PR URL, branch, and blocked head SHA;
- first-line base/rebase instruction when needed;
- blocker and failure mechanism;
- required change;
- scope/non-goals;
- relevant invariants;
- evidence expected;
- return contract requiring the new PR head SHA.

The implementation/fix agent should update the existing PR unless the coordinator explicitly requires a replacement PR.

Then state the after-fix disposition as exactly one of:

- `FOCUSED RECHECK`
- `MECHANICAL CHECK ONLY`
- `NEW SPECIALIST REVIEW`
- `NORMAL MERGE REVIEW`

If your instructions change later, reissue the entire replacement handoff; never provide an addendum that requires Marco to combine messages.

### After a blocker fix

If a prior review already established the surrounding design and isolated a concrete blocker, the next review should normally be a **focused recheck of that blocker on the new exact PR head**, not a fresh broad review.

Reopen broader review only when the fix materially changes the previously accepted design or exposes a new concrete merge-critical uncertainty.

### If verdict is merge

State `VERDICT: MERGE` clearly, identify the exact approved/reviewed PR head SHA, provide the coordinator handoff, and give `TESTS TO RUN` exactly as required above. Never ask Marco to rerun evidence already supplied by the implementation agent.

Do not issue `MERGE` as an integration instruction and do not tell Marco to merge directly.
