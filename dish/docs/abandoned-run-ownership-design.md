# Abandoned agent runs: pre-release recovery and long-term ownership redesign

This is Revision 13 of the design, converted from the source `.docx` for repository tracking.
Dates and "current" language below reflect that source and are not re-derived from later commits.

| Field | Value |
|---|---|
| Revision | 13 |
| Date | 30 July 2026 |
| Part I status | **Pre-rollout implementation candidate — being implemented now** |
| Part II status | **Post-rollout draft — needs work, intentionally not reviewed or approved** |
| Supersedes | V12 split design |
| Source basis | V12, repository architecture/runtime contract, seven passing orphaned-run characterization tests, and narrow launch-scope review |

> **Decision.** Split the work. Part I solves the demonstrated pre-release failure: a
> permanently lost chat run can strand an open Planning, Research, or Verification attempt.
> Part II describes the larger attempt/session ownership redesign only as a future draft. It is
> not a launch dependency and must not be implementation-reviewed with Part I.

## Reading rule

- Review and refine Part I only; it is pre-rollout work in progress.
- Do not treat Part II as approved design, implementation scope, or a source of launch
  requirements; it is post-rollout only.
- Characterization evidence overrides earlier assumptions about lease expiry, holds, and
  Verification takeover.

# Part I — pre-rollout: permanent run abandonment

> **Pre-release principle.** `recover-lease` means the same run is returning. `abandon-operation`
> means that run is permanently gone. The replacement never inherits the old run identity; Dish
> retires the old attempt and creates a fresh attempt when restart is required.

## 1. Proven problem

The characterization suite contains seven passing tests and leaves the existing behavior
unchanged. It proves:

| Case | Observed behavior | Durable blocker |
|---|---|---|
| Planning / Research | The original run resumes after `recover-lease`. A fresh run still cannot start or prepare. | `operations.run_id` |
| Verification | The original verifier resumes after recovery. A fresh run cannot restart the bound cycle or reject. | `operation_actor_facts` and `verification_cycles.run_id` |
| Evidence / Human hold | The hold already releases the lease. The original run resumes; a fresh run remains unauthorized. | Existing durable run/actor binding |
| Large reject after `3d9a5b6` | The attestation paraphrase bug is fixed. A genuinely different verifier run remains rejected for verifier authority. | `verification_cycles` verifier/run proof |

Therefore the defect is not ordinary lease expiry. The defect is the absence of an explicit path
for declaring a durably bound run permanently unavailable.

## 2. Operator contract

Marco makes only one decision: is the original run returning? Dish handles all stage-specific
behavior in code.

| Situation | Command | Meaning |
|---|---|---|
| Same chat/run will continue | `dish-admin recover-lease OPERATION_ID --reason "..."` | Release an expired actor lease. Ownership is unchanged. |
| Chat/run is permanently gone | `dish-admin abandon-operation OPERATION_ID --lease-id LEASE_ID --reason "..."` | Retire the exact run attempt and produce the safe next workflow state. |

> **Operator simplicity.** Marco does not choose Planning rollback, Research restart,
> Verification-cycle replacement, or committed-route finalization. The command reads durable
> evidence and selects the stage policy. Contradictory evidence returns a blocked result instead
> of asking Marco to guess.

## 3. Pre-release scope

- Planning, Research, and Verification attempts whose owning run is permanently unavailable.
- Exact expired or administratively released actor lease. A live actor lease is outside the
  pre-release abandonment path.
- Fresh same-stage successor only when the task already matches the exact safe baseline and no
  partial or uncertain external effect exists.
- Existing recovery/finalization when a durable handoff, rejection route, hold route, or
  submission is already committed.
- Fail-closed manual reconciliation when the baseline does not match, an effect is partial or
  uncertain, or external state is contradictory.

## 4. Explicit non-goals

- No generic transfer of `operations.run_id` or verifier actor facts to a new run.
- No public revision feature or intentional-revision transition.
- No attempt/session ownership redesign in the launch patch.
- No historical release execution, release fingerprint deployment fence, or multi-release engine.
- No completed-request authority projection. Historical replay may remain historically accurate;
  every mutation still revalidates current operation and target authority.
- No launch-time compensation of partial or uncertain writes, movements, corrections, or
  decisions. Those states block for manual reconciliation.
- No guessing for legacy Verification leases without provable cycle identity.

## 5. Exact abandonment authority

`abandon-operation` must validate all of the following before any external mutation:

- The source operation is open and is the current actionable operation for the task.
- The supplied lease exists in append-only lease history and belongs to the source operation,
  owner, and run.
- For Verification, the exact cycle is proven by lease context when available, otherwise by a
  unique verifier-run binding; ambiguity blocks.
- The targeted lease is the latest actor lease attempt for the current actionable operation. Any
  later actor lease, even if already released, blocks abandonment.
- No later operation or successor exists for the task, and the targeted actor lease is expired or
  already administratively released.
- A live actor lease cannot be abandoned by the pre-release command; wait for expiry or use the
  existing stale-lease administrative release path first.
- No other abandonment for the same source operation/lease is in progress.

The exact abandoned owner/run is permanently excluded from claiming any successor created by that
abandonment. A new run under the same connected client may claim it.

## 6. Stage policy handled by code

| Stage | Below committed checkpoint | At or after committed checkpoint | Contradictory evidence |
|---|---|---|---|
| Planning | Require the live task to already match the exact pre-Planning baseline and Planning placement; cancel the source and create a fresh Planning operation in `prepare_required`. | Finish and preserve the confirmed Research handoff. | Block if baseline/placement differs or any effect is partial, uncertain, or contradictory. |
| Research | Require the live task to already match the exact pre-Research baseline and Research Queue placement; cancel the source and create a fresh Research operation in `prepare_required`. | Finish and preserve Verification handoff or confirmed non-material completion. | Block if baseline/placement differs or any effect is partial, uncertain, or contradictory. |
| Verification | Require the confirmed candidate and placement to match the exact safe pre-decision frontier, with no partial correction, rejection, approval, hold, or movement effect; cancel the source and create a fresh Verification operation/cycle. | Preserve or finish committed rejection/hold/submission routes. | Block if any decision or movement effect is partial, uncertain, or contradictory. |

## 7. Safe pre-release boundaries

The implementation recognizes three outcomes. This keeps the launch patch broad enough for all
stages without pretending every partial external failure is automatically recoverable.

| Outcome | Required evidence | Behavior |
|---|---|---|
| `restart_prepared` | The prior stage result is not committed; the live task already equals the exact safe baseline and placement; no partial or uncertain effect exists. | Cancel source as `agent_abandoned`; create the exact successor and return an exact-target start. Do not perform compensating writes or movements. |
| `committed_finalized` | The stage checkpoint or route is durably and externally confirmed. | Finish existing recovery suffix; no replacement attempt unless the next stage requires one. |
| `blocked_manual_reconciliation` | Baseline/placement mismatch, partial effect, uncertain effect, unsupported frontier, or contradictory live state. | Retain the task fence; return the exact `reconcile-abandonment` admin command and relay instructions; do not guess or compensate automatically. |

### 7.1 Blocked relay contract

When abandonment blocks, Dish returns the exact executable admin command, tells the connected
agent to relay it to Marco and wait for confirmation, then requires the agent to refresh the
authoritative Dish action before continuing. The agent must not infer the command result or
construct a continuation itself.

## 8. Minimal durable model

### 8.1 `abandonment_attempts`

| Field | Purpose |
|---|---|
| `abandonment_id` | Stable idempotency and audit identity. |
| `task_gid` / `source_operation_id` | Exact workflow case and source attempt. |
| `source_lease_id` / `abandoned_owner_id` / `abandoned_run_id` | Exact abandoned actor authority. |
| `attempt_cycle_id` | Exact Verification attempt cycle, nullable outside Verification. |
| `status` | `started` \| `blocked_manual_reconciliation` \| `awaiting_successor_claim` \| `completed`. |
| `outcome` | `restart_prepared` \| `restarted` \| `committed_finalized` \| `route_preserved`. |
| `successor_operation_id` / `successor_cycle_id` | Exact replacement target when created. |
| `current_execution_id` | Existing operation-execution authority used by the active admin invocation. |
| `reason` / `timestamps` / `latest_result_json` | Operator reason, audit, and stable command response. |

### 8.2 `operation_successions`

Create one immutable edge only when abandonment creates a replacement operation:

```
source operation --agent_abandonment--> successor operation
```

- One successor per abandoned source operation.
- Source and successor share `task_gid` and differ.
- The source is cancelled with `terminal_outcome=agent_abandoned`.
- The successor is the only forward actionable operation.
- The edge records the exact selected source content version and successor-owned baseline.

### 8.3 Verification-cycle outcome

- Only an incomplete cycle belonging to the abandoned verifier may become abandoned.
- Approved and rejected cycles remain immutable.
- A fresh successor cycle has no verifier run, agent, review binding, attestation, or decision.

## 9. Exact-target abandonment starts and continuations

A replacement start must target the exact successor created by the abandonment. This prevents a
delayed action from claiming a later attempt.

| Abandonment continuation | Required exact target |
|---|---|
| Planning / Research successor | `prepared_operation_id` |
| Verification successor | `target_operation_id` and `target_cycle_id` |
| Route-preserved Verification continuation | `target_operation_id` and `target_cycle_id` |

- Planning and Research use the prepared target only for an abandonment-created successor. Every
  Verification start returned by abandonment, including a route-preserved continuation on an
  existing operation, must carry exact operation and cycle targets.
- Validation occurs before external reread and again in the authority transaction. A delayed
  Verification continuation must never retarget to a later cycle.
- The exact abandoned owner/run is rejected even with correct target IDs.
- Because this is a fresh attempt, it uses the deployment-current governed release. No historical
  release snapshot is required.

## 10. Transaction and crash rules

1. Create/load the abandonment and claim the source operation through `CurrentWorkflowService`
   and the existing operation-execution claim subsystem.
2. Journal committed-route recovery or finalization effects using existing write/movement attempt
   evidence. The launch path does not initiate compensating mutations for partial or uncertain
   effects.
3. In one final SQLite transaction: resolve source steps/execution, terminalize or finalize the
   source, create successor/baseline/cycle when needed, update abandonment, release the exact
   lease, and publish the next action.
4. A crash exposes either the complete old state or the complete final state.
   `reconcile-abandonment` resumes the durable abandonment record; it does not invent a new
   workflow decision.
5. Post-succession repair effects belong to the successor, never the cancelled source.

## 11. Legacy and compatibility rule

> **Hard launch rule.** The command supports a legacy attempt only when the exact lease
> owner/run and, for Verification, exact attempt cycle can be proven from durable records.
> Ambiguous legacy attempts fail closed into the existing maintenance/recovery path. The
> migration must not synthesize lease kind, cycle, or ownership history.

New Verification leases should record `context_cycle_id` at acquisition. This improves future
abandonment but is not used to rewrite old history.

## 12. Replay behavior for the pre-release patch

- Pending admin requests receive stable blocked or completed abandonment results.
- Historical completed agent request envelopes are not rewritten.
- A stale historical response cannot grant mutation authority: source status, exact target,
  lease/run eligibility, and abandonment fences are revalidated on every mutation.
- A later cleanup may overlay current authority on historical replay, but that is not required to
  remove the demonstrated dead end.

## 13. Required tests

- Existing seven characterization tests remain green before and after the feature.
- Same-run `recover-lease` behavior remains unchanged for Planning, Research, Verification, and
  holds.
- Clean abandoned Planning and Research attempts create fresh same-stage successors.
- Abandoned Verification before decision creates a fresh unbound Verification operation/cycle.
- Committed Planning/Research handoff and committed rejection/hold/submission routes are preserved
  truthfully.
- Contradictory or uncertain external effects block without source mutation.
- An older released actor lease cannot be abandoned after a later actor lease attempt exists; a
  live lease is rejected.
- The abandoned owner/run cannot claim the successor; a fresh run can.
- Every Verification continuation returned by abandonment, including a route-preserved cycle,
  requires exact operation and cycle targets and cannot retarget to a later cycle.
- Prepared successor targets cannot be retargeted on delayed replay.
- Crash injection around each external effect and final transaction produces only complete
  pre/post states.
- Legacy ambiguous Verification attempt refuses abandonment rather than guessing.
- Large reject continues to inherit persisted `independence_attestation` while rejecting a
  different verifier run.

## 14. Implementation surface

| Area | Pre-release work |
|---|---|
| `dish_tool/admin.py` / `admin_cli.py` | `abandon-operation` and `reconcile-abandonment` orchestration; stage policy; exact command results. |
| `dish_tool/database_schema.py` | abandonment and succession records; terminal outcome; cycle outcome; uniqueness and immutability constraints. |
| `dish_tool/database.py` | transaction-scoped creation/finalization helpers and successor baseline. |
| `dish_tool/operation_execution.py` | reuse existing execution claim/recovery authority for admin mutation. |
| `dish_service/leases.py` | Exact expired/released actor-lease validation, latest-actor-attempt check, abandoned-principal fence, and Verification cycle context for new leases. |
| `dish_service/application.py` | task abandonment fence and prepared-successor routing. |
| `dish_service/command_spec.py` / client / CLI | Prepared Planning/Research target plus exact operation/cycle targets for every Verification continuation returned by abandonment. |
| `dish_tool/step5`/`step6`/`step7`/`step8`/`step9` | role-specific start binding, checkpoint classification, Verification frontier and existing recovery reuse. |
| tests/docs | characterization, fault injection, runtime contract, known issues, operator runbook. |

## 15. Pre-release acceptance

- A permanently lost chat run cannot strand Planning, Research, or Verification.
- `recover-lease` remains same-run recovery and never transfers ownership.
- Marco runs one stage-agnostic command; code selects the correct stage behavior.
- No old actor identity is transferred or allowed to claim the replacement.
- Committed work is preserved; only clean baseline-matching states are restarted; partial,
  uncertain, or contradictory states are blocked.
- The launch patch does not depend on the long-term ownership redesign.

# Part II — post-rollout: long-term attempt/session ownership redesign

> **Draft — needs work — do not review or implement.** This section records the direction only.
> It is intentionally incomplete, is not approved, and must not add requirements to the
> pre-release implementation. Re-open it after the abandonment patch has shipped and production
> evidence is available.

## 16. Long-term problem statement

Dish currently makes an ephemeral chat run part of durable attempt ownership. The lease answers
temporary liveness, while `operations.run_id`, actor facts, and Verification cycle bindings
effectively make that run the permanent executor. Explicit abandonment works around a dead session
by creating a new attempt. The long-term model should represent attempts and execution sessions
separately.

## 17. Candidate conceptual model

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

## 18. Questions that intentionally remain open

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

## 19. Likely durable concepts

| Concept | Tentative purpose |
|---|---|
| `workflow_attempt` | Durable stage attempt independent of any one chat session. |
| `attempt_sessions` | Append-only sessions with owner, run, role, state, start/end, and replacement reason. |
| `session_leases` | Short-lived mutation authority attached to one session. |
| `session_replacements` | Explicit authorization edge from abandoned/superseded session to replacement session. |
| `effect_actor_session_id` | Exact session provenance for writes, movements, decisions, and actor facts. |
| request current-authority view | Separation between immutable historical result and present workflow authority. |

## 20. Relationship to the pre-release patch

| Pre-release component | Long-term disposition |
|---|---|
| Explicit abandonment declaration | Retained. A session can still be permanently abandoned. |
| Exact lease/owner/run audit | Retained and becomes session identity evidence. |
| Checkpoint and external-effect classification | Retained. |
| Immutable abandonment audit and successor lineage | Retained where a new attempt is still required. |
| Always create a new operation for replacement | May be reduced: safe frontiers might authorize a replacement session within the same attempt. |
| `operations.run_id` as durable owner | Replaced by attempt/session association. |
| Verifier run stored directly as cycle authority | Likely replaced or supplemented by verifier-session identity. |
| Prepared successor target | Retained when replacement requires a new attempt; unnecessary when a safe same-attempt session replacement is authorized. |

## 21. Rough implementation scale

These estimates assume AI performs most coding and a human reviews authority and migration
behavior:

| Work | Rough AI-assisted effort | Risk |
|---|---|---|
| Pre-release abandonment patch | Approximately half a day to one focused day, then a review/fix pass. | Medium: sensitive recovery code, but bounded. |
| Long-term attempt/session redesign after design is settled | Approximately one to three focused days for core implementation and migration/test iterations. | High: core authority and provenance model changes. |

The long-term change is not weeks of code generation, but it should not be run as one unattended
merge. The difficult part is proving authority, replay, migration, and crash behavior rather than
writing the schema or handlers.

## 22. Draft exit criteria before future review

- Production evidence shows which abandonment frontiers are common and which could safely use
  same-attempt session replacement.
- A complete attempt/session authority matrix exists for Planning, Research, Verification, holds,
  and terminal routes.
- Request replay semantics distinguish historical result from current authority.
- Migration behavior for existing operations is explicit and fail-closed.
- Fault-injection and concurrency tests are defined before implementation approval.

## 23. Status

> **Intentionally parked.** Part II remains a draft marked NEEDS WORK. Do not iterate it during
> pre-release review. Re-open only after Part I is implemented, characterized in real testing, and
> no longer blocking rollout.

# Appendix A — source basis

- V11 and V12 design documents dated 30 July 2026, including the narrow clean-frontier launch
  review applied in V13.
- Seven passing characterization tests in `tests/test_dish_orphaned_run_characterization.py`; full
  suite reported as 1,013 tests green.
- Observed durable gates: `operations.run_id`, `operation_actor_facts`,
  `verification_cycles.run_id`, and `_may_claim_missing_lease`.
- Commit `3d9a5b6022185653858baf10b148edede2c138e7`, which removed re-supply of
  `independence_attestation` for Large rejection without changing verifier-run authority.
- Current Dish architecture, runtime contract, lease recovery, operation execution, Planning,
  Research, Verification, and admin discard/recovery paths.
