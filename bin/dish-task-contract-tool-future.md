# Dish Task Contract Tool — Future / v2+ Ideas

**Purpose:** Holds everything about the dish-task contract tool that is NOT part of the v1
design — the v1b enforcement flip, v2 candidate features, and ideas considered and rejected
outright. Split out of `dish-task-contract-tool.md` so that doc can stay focused on exactly
what v1 needs to exist and work.

**Status:** Not authorized for implementation. Nothing here is scheduled; items move into the
v1 doc (or a future v1.x/v2 doc) only when Marco explicitly decides to build them.

## Versioning plan

The tool is built and rolled out in stages, scoped to what the evidence in
`dish-task-contract-change-plan.md`, `dish-task-contract-incident-log.md`, and
`dish-task-contract-review-log.md` actually requires. Nothing beyond v1a/v1b is built until real usage
data justifies it.

**v1a — build and soft-launch.** The full guarded path (`prepare` / `approve` / `reject` / `submit` /
`contract-admin recover`) is implemented, tested, and usable end-to-end against live tasks — it
performs real Asana writes through the guarded, token-protected path. What v1a does *not* do is make
this path mandatory: the existing generic Asana CLI still works for managed tasks, and its managed-task
check runs in advisory/log-only mode (see Contract-managed task registry, Logging and observability).
This proves the hardest, most novel logic — the structural validator against the contract's manifest,
the exact-content hash binding, the submission state machine, and uncertain-outcome recovery — under
real conditions, without the operational risk of a validator bug or an over-sensitive staleness check
blocking a live cook. It also produces the usage data needed to decide v1b's timing and v2's scope.

**v1b — enforce.** Once v1a's validator has run clean against real usage and the `modified_at`
staleness behaviour has been empirically confirmed (see Content hashing), the generic CLI's
managed-task check is flipped from advisory to blocking. No new mechanism is added at this stage —
v1b is a configuration flip on v1a's own logged evidence, not new code.

**v2 — add once v1a data justifies it.**

* The two-failed-pass stop rule (`dish-task-contract.md` lines 206-209) — real contract text, but no
  incident shows it failing in practice; v1a's rejection-rate logging is exactly the evidence needed
  to decide whether to build it. Cheap to add once needed (a counter and a gate).
* The small-change (`small`/Local) carelessness speed bump — Marco's standing concern (see Out of
  scope (all versions) in `dish-task-contract-tool.md`): an honest agent carelessly mis-declaring a
  material change as `small`, not a malicious one gaming the system. v1a's logging of what real
  `small`-declared diffs actually touch and how large they are is the input needed to design the
  trigger condition, which is currently undecided.
* Bounded direct-dependency surfacing (see Direct dependencies) — already scoped in the change plan as
  advisory and non-blocking; natural to add once the core write path is proven.
* Token/submission replacement as a distinct action from `contract-admin recover` — only worth building
  if recovery proves insufficient in real use.

**Dropped, not deferred.** These were considered and rejected outright, not postponed:

* Verifier in-place editing and any author-reassignment bookkeeping (`last_content_author` and
  related). No incident motivates letting a verifier edit the note at all; a verifier who finds a
  problem rejects it and the editor resubmits (see Workflow). This removes an entire dimension of
  state the design previously carried.
* `--confirm-independent-review` as a separate required flag, and any dedicated "self-verification
  collision" detection alongside it. The opposite-family requirement on `approve` already makes
  `editor_agent == verifier_agent` structurally unreachable — routing rejects it before any collision
  check could run — so a comparison built to catch that case is dead code, not a protection. The real
  residual risk (one session dishonestly declaring `claude` for editing and `gpt` for verification) is
  exactly the "trusted, not authenticated" limit already stated in Scope, and no mechanical check
  catches it; claiming one does would be worse than naming the gap.
* A cached, authoritative `managed_tasks` table. Management is always resolved live (see
  Contract-managed task registry); a cache that isn't authoritative isn't worth maintaining.
* A distinct adversarial self-review mechanism. The review log is explicit that this "was an assistant
  recommendation and was not approved in the enforcement handoff" and creates no implementation
  requirement. Stays out unless Marco separately approves it.
* Cryptographic identity authentication, recursive dependency audits, automatic migration of existing
  tasks, a multi-user/remote trust service (see Out of scope) — no new reason to revisit these.

## Direct dependencies (v2)

Deferred; not built in v1a/v1b. Dependency surfacing is advisory and must not block submission status
in any version. A later scanner may surface only bounded direct candidates: exact task-GID references;
explicit Asana links; exact task-name references; clearly named planning documents. It must not
recursively audit dependencies or decide semantic impact.

## Open decision: small-change carelessness (v2)

Marco's concern is an honest agent carelessly mis-declaring a material change as `small`, not a
malicious one gaming the system. The fix should be a deterministic speed bump, not independent
verification for every `small` change — but its trigger condition, what it actually requires of the
agent, whether it's a hard block or a warning, and where it lives are all undecided. v1a's logging of
real `small`-declared diffs is the intended input for designing this (see Versioning plan, Logging and
observability).
