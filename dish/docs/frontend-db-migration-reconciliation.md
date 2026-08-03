# Frontend database migration reconciliation

Status: reconciled against checked-in Alembic head `0012_task_grant_semantic_identity`; production
activation and frontend support migrations remain pending.

This document turns Gate A/B database dependencies into a migration handoff. It records what the
current Stage A PostgreSQL model already proves, what it cannot prove, and which frontend-owned support
state may be added without becoming task or workflow authority. The machine-readable counterpart is
`../frontend/contracts/stage3-read-contract.json`.

## Reconciliation boundary

The checked-in chain is `0001_stage_a_baseline` through
`0012_task_grant_semantic_identity`. The current production-change ledger still contains a worktree
marker for the Verification-hold change and the database migration contract does not claim production
cutover. Therefore:

- schema statements here describe the checked-in production candidate, not live production;
- the final migration head and ledger commit must be recorded again after the database rollout;
- no frontend stage may infer that a table is authoritative merely because its SQLAlchemy model exists.

## Existing canonical inputs usable by a future board query

The `0012` model set already contains the necessary canonical joins for basic board membership:

| Result need | Current canonical source | Reconciliation result |
|---|---|---|
| one active authority generation | `authority_generations.status='active'` | Present, with one-active partial unique index |
| active registry | `active_section_registries` | Present |
| ordered section labels and roles | `section_registry_entries` joined to governed section/project | Present; label ambiguity still requires application validation |
| non-retired task | `dish_tasks.existence_state` | Present; isolated-task eligibility remains a decision |
| incomplete task | `current_task_completion.completed=false` | Present; existing `section_tasks()` does not apply it |
| current project membership | `current_task_project_memberships.is_member=true` | Present |
| current section placement | `current_task_section_placements` | Present |
| current title/body identity | `task_authority_heads` to current activation/version | Present |
| one open operation | `workflow_operations.lifecycle='open'` | Present, with one-open-operation support |

This is enough to design a set-oriented frontend board query after Gate B decisions. It is not enough
to serialize `PostgresReadModel.section_tasks()` or `task_view()` directly.

## Attention reconciliation

### Exact with current durable facts

- expired lease: active lease with `expires_at <= evaluation_time`, plus an explicitly persisted
  `state='expired'` only where the governing lifecycle says it remains presentation-relevant;
- open Evidence hold: `evidence_holds.state='open'`;
- awaiting human review: `human_review_requirements.state='open'`;
- active abandonment: `abandonment_attempts.state='active'`;
- active succession: a published `operation_succession_edges` row attached to the accepted active
  abandonment;
- open projection drift: `projection_drift_events.state='open'`.

### Not exact in `0012`

- lease **invalid** and **contested** are not named states or relations. `released` and `recovered`
  are ordinary terminal states and cannot be relabelled as attention;
- Verification **failed** and **disputed** are not a closed frontend predicate. The cycle lifecycle is
  closed, but `outcome` does not by itself establish the approved product semantics;
- there is no task-scoped durable relation naming an unresolved recovery requirement;
- projection delayed/failed/unknown/unavailable/current/not-configured needs one accepted reducer,
  delay threshold, readiness input, and precedence.

Those gaps require either exact frontend-owned support derived transactionally from governing facts or
a targeted product-contract amendment. A query author may not fill them from phase text, free text,
operation failure alone, or browser inference.

## Required frontend support migration

These are frontend-owned support records, not additions to workflow authority.

### Stage 2 security support

- `frontend_security_generations`: current global session-security generation and rotation evidence;
- `frontend_sessions`: non-recoverable session verifier, fixed issue/expiry, revocation, generation,
  and restore-fence binding;
- `frontend_login_limiter_buckets`: durable peer/global failure-window state;
- `frontend_security_audit_events`: bounded security outcomes without credentials or task content;
- `frontend_password_state`: current Argon2 verifier metadata and security-generation binding, or an
  equivalent transactionally rotatable owner.

The restore fence itself cannot live solely in the restored PostgreSQL database. The migration must
store only its binding to an independently current value.

### Stage 3 read support

- `frontend_route_identities`: bounded environment/type-scoped browser identities mapped to canonical
  task or section identity, with uniqueness, retirement, normalization, and rotation rules;
- `frontend_recovery_requirements`: task-scoped named open/resolved recovery support derived from the
  governing recovery transition, if the approved `recovery_required` product meaning is retained.

A cursor-handle table is conditional on selecting server-side cursor handles. A materialized frontend
read projection is conditional on query-plan evidence. Neither is required merely for convenience.

## Existing index evidence and likely additions

Existing primary/unique/partial indexes already support active generation, active projection epoch,
one open workflow operation, one active task actor lease, active task projection mapping, active
abandonment, and registry/current-state identities. Before adding frontend indexes, inspect the final
PostgreSQL plans to avoid duplicates.

Likely plan-driven additions remain:

- partial incomplete-task lookup by generation/task where `completed=false`;
- active project-membership lookup by generation/project/task where `is_member=true`;
- active-registry placement lookup by generation/registry/section/task;
- task-scoped open hold/human-review lookup;
- task-scoped lease state/expiry lookup;
- task projection event/drift lookup by generation/task/state/time;
- indexes owned by any accepted route-identity or recovery-support tables.

No index is approved solely by this list. The Stage 3 package must include representative
`EXPLAIN (ANALYZE, BUFFERS)` and fixed-query-count evidence against the final PostgreSQL schema.

## Decisions that still block query implementation

1. Are `dish_tasks.existence_state='isolated'` tasks eligible for board/detail display?
2. Is lease attention narrowed to expired, or will exact invalid/contested support be introduced?
3. Which Verification lifecycle/outcome/review facts mean failed and disputed?
4. What is the complete projection reducer, delay threshold, and readiness source?
5. Are browser route identities stored aliases or a stable cryptographic encoding, and how do they
   survive routine key rotation without exposing raw IDs?
6. Are cursors stateless signed envelopes or bounded server-side handles?

These are the only remaining decisions that should affect the migration shape before Stage 3. Board
query code should wait for them; test vectors and query-bound fixtures can be prepared now.

## Final rollout reconciliation record

Before Gate B review, fill in:

- final Alembic head:
- exact source commit/release:
- production-change ledger closed through:
- target environment schema fingerprint:
- Stage 2 frontend support migration revision:
- Stage 3 frontend support migration revision:
- accepted decisions 1–6 above:
- final indexes and query-plan evidence location:
- reviewer and review date:
