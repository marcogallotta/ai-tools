# Post-merge Review recovery

Use this path only after an explicit Review request targets a PR that GitHub authoritatively reports as already merged. `Already merged` is context, not a terminal Review answer.

The reviewer first performs the bounded safety pass and records exactly one result: `SAFE ENOUGH`, `SERIOUS DEFECT FOUND`, or `UNABLE TO DETERMINE`. Then persist and route the exact merged-head full-review obligation:

```sh
python scripts/pr_post_merge_review.py \
  --pr-number <N> \
  --thin-result '<RESULT>' \
  --thin-summary '<bounded safety summary>'
```

The command requires one existing owning Asana task. It creates or reuses one incomplete subtask for repository + PR + exact merged head, writes and re-reads a GitHub linkage marker, and dispatches the existing Review Workspace Agent with an obligation-specific idempotency key. `SAFE ENOUGH` never completes the obligation. `SERIOUS DEFECT FOUND` additionally creates/reuses the bounded corrective Implementation owner immediately while the full Review remains open.

A completed obligation is historical evidence, not permanent satisfaction for future explicit Review requests. If no incomplete obligation remains for the same repository + PR + exact merged head, a later explicit request creates a fresh durable Review round with a new obligation key while preserving all completed rounds. Retries while that fresh round is still open reuse it, so at most one incomplete round exists for the exact merged head.

The full reviewer reviews the exact head that actually merged; later `main` movement is context only. The formal exact-head GitHub `COMMENT` Review must contain a normal `VERDICT: MERGE` or `VERDICT: BLOCK` and the exact `dish-post-merge-full-review:v1 key=<key> head=<sha>` marker supplied by the current obligation. Historical pre-merge Review and formal Reviews from completed post-merge rounds cannot satisfy the new obligation because they lack its new round identity.

The existing closed-PR lifecycle scan re-reads the durable Asana obligation and GitHub reviews. A matching full Review is written back with exact Review id/head/verdict before the obligation is marked complete. `BLOCK` creates or reuses one bounded corrective Implementation owner; if the thin pass already created it, the full Review links to that same owner rather than creating parallel work.

Corrective source work uses a new Implementation branch/PR from exact current `main`; `ci/source-recovery.md` describes the fail-closed inverse helper when reversal is appropriate. Source reversal never proves database/runtime/deployment/external effects were recovered. Those remain separate recovery gates under their existing authorities.

This path adds no Review queue, scheduler, service, lifecycle database, merge authority, or direct-main shortcut. GitHub remains the Review artifact and normal Implementation → independent Review → Integration remains the corrective lifecycle.

Invoke the durable recovery entrypoint with `scripts/pr_post_merge_review.py`; it performs the obligation write/readback and routes the existing Review Workspace Agent when configured.
