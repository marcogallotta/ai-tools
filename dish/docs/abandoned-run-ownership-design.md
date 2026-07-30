# Abandoned agent runs: pre-release recovery and long-term ownership redesign

This is Revision 14, updated after Part I shipped. Dates below reflect original authorship and
are not re-derived from later commits.

| Field | Value |
|---|---|
| Revision | 14 |
| Date | 30 July 2026 |
| Part I status | **Implemented.** Landed across eight staged commits ending in `a5753d6` (admin workflow exposure) and `516ef5b` (pre-construction Research reject lease reacquisition). See `docs/architecture.md` and `docs/runtime-contract.md` for current behavior; this section is a historical summary, not a live spec. |
| Part II status | **Reopened for review. Not ready for implementation.** Still a draft; review comments only. |
| Supersedes | V13 |
| Source basis | V13, repository architecture/runtime contract, seven passing orphaned-run characterization tests, narrow launch-scope review, and the shipped implementation. |

> **Decision.** Part I is implemented: a permanently lost chat run can no longer strand an open
> Planning, Research, or Verification attempt. Part II remains a future draft for the larger
> attempt/session ownership redesign; it is reopened for review but is not launch scope and must
> not be implemented.

## Reading rule

- Part I is historical record. For current behavior, read `docs/architecture.md` and
  `docs/runtime-contract.md`, not this section.
- Part II may be reviewed and commented on, but it is not approved design, implementation scope,
  or a source of launch requirements.

# Part I — implemented: permanent run abandonment

> **Shipped principle.** `recover-lease` means the same run is returning. `abandon-operation`
> means that run is permanently gone. The replacement never inherits the old run identity; Dish
> retires the old attempt and creates a fresh attempt when restart is required.

## Summary

- **Problem.** The original characterization suite proved that after `recover-lease`, only the
  original run could resume Planning, Research, or Verification; a fresh run stayed rejected with
  no path forward even when the original was permanently gone.
- **Operator contract.** Marco decides only whether the original run is returning
  (`dish-admin recover-lease`) or permanently gone (`dish-admin abandon-operation OPERATION_ID
  --lease-id LEASE_ID --reason "..."`). Code selects the stage-specific outcome from durable
  evidence; Marco never chooses Planning rollback, Research restart, or Verification-cycle
  replacement directly.
- **Outcomes.** `restart_prepared` (clean baseline match → fresh same-stage successor),
  `committed_finalized` (preserve/finish an already-committed route), or
  `blocked_manual_reconciliation` (partial/uncertain/contradictory state → fail closed, returning
  the exact `dish-admin reconcile-abandonment ABANDONMENT_ID` command).
- **Durable model.** `abandonment_attempts` and `operation_successions` tables; a task-level
  connected-mutation fence while abandonment is active; crash-safe reconciliation reclaims the
  same operation execution rather than chaining a new one (`claim_abandonment_execution`).
- **Authority invariants that still apply.** The targeted lease must be the latest actor attempt
  and be expired or administratively released — a live lease cannot be abandoned. The abandoned
  owner/run can never claim the resulting successor. A legacy attempt without a provable lease
  owner/run (and, for Verification, a provable cycle) fails closed rather than guessing.
- **Final piece.** Pre-construction Research `reject` (Evidence/Human-Review routes) is
  stage-actor authority, not verifier authority: the original Research run may reclaim a missing
  lease there the same way it can for `prepare`, without transferring ownership to a different
  run.

## Acceptance (met)

- A permanently lost chat run cannot strand Planning, Research, or Verification.
- `recover-lease` remains same-run recovery and never transfers ownership.
- Marco runs one stage-agnostic command; code selects the correct stage behavior.
- No old actor identity is transferred or allowed to claim the replacement.
- Committed work is preserved; only clean baseline-matching states are restarted; partial,
  uncertain, or contradictory states are blocked.
- The launch patch did not depend on the long-term ownership redesign in Part II.

# Part II — post-rollout: long-term attempt/session ownership redesign

> **Reopened for review — not ready for implementation.** This section records the direction
> only. It is incomplete and not approved, and must not add requirements to the shipped Part I
> implementation. Review and comment freely; do not treat any part of it as implementation scope
> until it is explicitly promoted.

## Long-term problem statement

Dish currently makes an ephemeral chat run part of durable attempt ownership. The lease answers
temporary liveness, while `operations.run_id`, actor facts, and Verification cycle bindings
effectively make that run the permanent executor. Explicit abandonment works around a dead session
by creating a new attempt. The long-term model should represent attempts and execution sessions
separately.

## Candidate conceptual model

```
workflow stage attempt
-> execution session A (run_id A, lease A)
-> execution session B (run_id B, lease B)
-> terminal stage result
```

- The attempt owns durable stage intent, input baseline, checkpoint, and result.
- A session owns one connected run, its lease, request/replay context, and the effects it
  performed.
- A lease authorizes active mutation by one session; it does not define permanent attempt
  ownership.
- Session replacement is explicit, authorized, audited, and allowed only at safe frontiers.

## Questions that intentionally remain open

- Can a new Planning or Research session continue the same attempt before any external effect, or
  should it always create a new attempt?
- Which actor identity belongs to the attempt, which belongs to each session, and how is
  provenance shown after replacement?
- Can a new verifier ever continue the same cycle after review binding, or must verifier-session
  loss always create a fresh cycle?
- How do completed and pending request replays expose historical results versus current
  authority?
- How are session replacement permissions represented and who may authorize them?
- What migration is safe for existing operations whose run/session history was not recorded
  separately?
- How do Human/Evidence holds, reconnects, client upgrades, and schema changes affect session
  state?

## Likely durable concepts

| Concept | Tentative purpose |
|---|---|
| `workflow_attempt` | Durable stage attempt independent of any one chat session. |
| `attempt_sessions` | Append-only sessions with owner, run, role, state, start/end, and replacement reason. |
| `session_leases` | Short-lived mutation authority attached to one session. |
| `session_replacements` | Explicit authorization edge from abandoned/superseded session to replacement session. |
| `effect_actor_session_id` | Exact session provenance for writes, movements, decisions, and actor facts. |
| request current-authority view | Separation between immutable historical result and present workflow authority. |

## Relationship to the shipped Part I patch

| Part I component | Long-term disposition |
|---|---|
| Explicit abandonment declaration | Retained. A session can still be permanently abandoned. |
| Exact lease/owner/run audit | Retained and becomes session identity evidence. |
| Checkpoint and external-effect classification | Retained. |
| Immutable abandonment audit and successor lineage | Retained where a new attempt is still required. |
| Always create a new operation for replacement | May be reduced: safe frontiers might authorize a replacement session within the same attempt. |
| `operations.run_id` as durable owner | Replaced by attempt/session association. |
| Verifier run stored directly as cycle authority | Likely replaced or supplemented by verifier-session identity. |
| Prepared successor target | Retained when replacement requires a new attempt; unnecessary when a safe same-attempt session replacement is authorized. |

## Rough implementation scale

These estimates assume AI performs most coding and a human reviews authority and migration
behavior:

| Work | Rough AI-assisted effort | Risk |
|---|---|---|
| Long-term attempt/session redesign after design is settled | Approximately one to three focused days for core implementation and migration/test iterations. | High: core authority and provenance model changes. |

The long-term change is not weeks of code generation, but it should not be run as one unattended
merge. The difficult part is proving authority, replay, migration, and crash behavior rather than
writing the schema or handlers.

## Exit criteria before implementation review

- Production evidence shows which abandonment frontiers are common and which could safely use
  same-attempt session replacement.
- A complete attempt/session authority matrix exists for Planning, Research, Verification, holds,
  and terminal routes.
- Request replay semantics distinguish historical result from current authority.
- Migration behavior for existing operations is explicit and fail-closed.
- Fault-injection and concurrency tests are defined before implementation approval.

## Status

> **Reopened for review; not ready for implementation.** Comment and refine the direction here.
> Do not implement any part of Part II, and do not treat review activity as approval, until these
> exit criteria are met and the section is explicitly promoted out of draft.

# Appendix A — source basis

- V11–V13 design documents dated 30 July 2026, including the narrow clean-frontier launch review
  applied in V13, and the shipped implementation across dish-stage commits ending in `a5753d6`
  (admin workflow exposure) and `516ef5b` (pre-construction Research reject lease reacquisition).
- Seven passing characterization tests in `tests/test_dish_orphaned_run_characterization.py`,
  plus the full abandonment regression coverage added while implementing Part I
  (`test_dish_032_abandonment_persistence.py` through `test_dish_036_abandonment_admin_workflow.py`
  and the pre-construction Research reject coverage in
  `test_dish_031_actor_lease_attempt_context.py`).
- Observed durable gates: `operations.run_id`, `operation_actor_facts`,
  `verification_cycles.run_id`, and `_may_claim_missing_lease`.
- Commit `3d9a5b6022185653858baf10b148edede2c138e7`, which removed re-supply of
  `independence_attestation` for Large rejection without changing verifier-run authority.
- Current Dish architecture, runtime contract, lease recovery, operation execution, Planning,
  Research, Verification, and admin discard/recovery paths.
