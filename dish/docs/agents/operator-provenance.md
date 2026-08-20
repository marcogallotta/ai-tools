# Operator provenance

Use these bounded rules when authenticated service metadata or a consequential decision could be mistaken for human authorization.

## Actor attribution

Asana/GitHub actor, author, creator, committer, or similar account fields prove attribution only. When agents/tools can act through Marco's account, those fields do not prove that Marco physically acted, approved, transferred ownership, or supplied a Review verdict. Agent-authored durable writes keep Dish Agent role/host provenance.

For Development Workflow project `1217419962189616`, freshly read and apply `dish/docs/agents/development-workflow-asana-mode.md`; V3 returns `PROJECT MODE V3 REQUIRES UPDATED PROJECT SETTINGS / GPT ACTION PROTOCOL`.

## Decision provenance

Keep explicit human decisions, standing repository policy, agent inference/recommendation, runtime observation, and authenticated-account metadata distinct. Consequential policy/product/cutover decisions require durable human provenance; runtime or attribution data never silently becomes such a decision.

## Execution-state truth

Describe only the strongest state proved for the exact current attempt. A durable handoff is not launch; launch invocation is not acceptance; acceptance is not RUNNING. Asana placement, branch/PR existence, leases, locks, or prior-attempt evidence do not prove current execution liveness. Attempt N evidence never proves attempt N+1. GitHub absence is categorical only after successful exhaustive open-PR enumeration and exact assignment reconciliation; otherwise report UNKNOWN rather than `no PR`.

## Manual Worker Project profile

The manual multi-role Worker is a first-class generated ChatGPT Project profile at `dish/docs/chatgpt-projects/worker.md`. That generated file is maintained by the same `source.json` + `chatgpt_project_kernels.py` + manifest/check workflow as the standing role kernels. Do not extract a Worker profile from markers in this document and do not create a Worker-specific export/install ritual.

Worker is an execution/profile surface, not a ninth semantic standing role. It supports exactly four explicit semantic modes: Implementation, Code Review, Design Review, and Audit. Only one mode is active at a time, and each mode loads the current standing contract for that role. The profile never composes simultaneous authority.

The ordinary manual Project-chat route is self-sufficient. A fresh Worker can start from `Review PR #N`, resolve the current repository + owning task + PR + branch + exact head itself, and perform the requested Review without an API-launched Worker attempt, Workspace-Agent trigger, attempt/generation record, or provenance packet. Automated transport bookkeeping must not become a manual eligibility gate merely because it is absent.

### Deterministic manual Review -> BLOCK -> Implementation continuation

For the approved manual Worker operation, formal BLOCK -> repair is required, not optional:

1. Code Review binds repository + owning task + PR + branch + exact head H1 and remains read-only for candidate source.
2. `VERDICT: MERGE` is durably submitted/verified for H1, then the Worker stops.
3. `VERDICT: BLOCK` is durably submitted/verified for H1. That completes Review.
4. Without another Marco prompt, the **same Worker MUST explicitly leave Review and enter current Implementation/fix authority**.
5. Before semantic mutation, freshly load current `implementation.md` and re-read the live owning task, PR, branch, current head, and the exact formal BLOCK. Require the fix round to still match exact `(task, PR, branch, H1, block_review_id)`. Any movement/staleness means zero semantic mutation and current-state reclassification.
6. Fix only the accepted blocker/task scope on the same PR lineage, run the required Implementation evidence, publish the corrected candidate H2, and authoritatively read back branch/PR/head/evidence.
7. The Worker that authored H2 stops. It may not independently Review H2 while it remembers or can recover that it materially authored H2. A fresh Worker performs the next Review. Integration remains separate.

There is no second `fix it` / transition confirmation inside this deterministic Worker operation. Formal BLOCK + the approved Worker model is the transition trigger. This does not authorize queue pickup, automatic reviewer spawning, next-task progression, Integration, merge, deploy, or any unrelated correction.

## Manual Worker independence and automated provenance

Manual no-self-review is intentionally pragmatic and memory/context based. If the Worker currently remembers or can recover that it materially authored the candidate it is about to Review, it is not independent and must stop. Relabeling mode or continuing in the same chat does not erase known authorship. If genuine later compaction/recovery has removed that authorship history so it is no longer remembered or recoverable, Marco treats the Worker as effectively fresh for this manual cooperative-agent model. Do not add permanent chat taint, sentinels, launcher tokens, attempt-ID requirements, provenance archaeology, or a manual identity service solely to reconstruct forgotten manual history.

`dispatch_worker_durable`, `dish-worker-attempt:v1`, `dish-worker-authorship:v1`, cumulative automated attempt authorship, Workspace-Agent triggers, and related generation/idempotency machinery are transport-specific evidence for an actually automated route. When that route is used, its own exact attempt/candidate rules still apply and invalid bookkeeping may fail that automated route. Missing automated bookkeeping alone never blocks ordinary manual `Review PR #N` or the manual formal-BLOCK -> same-Worker fix continuation.

PR candidate identity remains repository + owning task + PR + branch + exact head SHA. Design candidate identity remains task + exact revision/generation + exact canonical snapshot/digest. Candidate movement invalidates old Review identity. Formal Review, role switching, source publication, fresh Review of a successor, and Integration/merge remain separate facts.
