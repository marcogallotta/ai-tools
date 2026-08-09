# Frontend database migration reconciliation

Status: reconciled against checked-in Alembic head `0033_frontend_security`; PostgreSQL remains a
non-authoritative dark-launch target and final production/runtime reconciliation remains pending.

This document turns Gate A/B database dependencies into a migration handoff. It records what the
current Stage A PostgreSQL model already proves, what it cannot prove, and which frontend-owned support
state may be added without becoming task or workflow authority. The machine-readable counterpart is
`../frontend/contracts/stage3-read-contract.json`.

## Reconciliation boundary

The checked-in chain is `0001_stage_a_baseline` through `0033_frontend_security`. Revision `0033` adds
frontend-only authentication/session support and does not change task/workflow authority. The database remains in dark-launch preparation and the migration
contract does not claim authority cutover. Therefore:

- schema statements here describe the checked-in production candidate, not live production;
- the exact deployed dark-launch migration head/runtime evidence must be recorded again before frontend HTTP activation;
- no frontend stage may infer that a table is authoritative merely because its SQLAlchemy model exists.

## Existing canonical inputs usable by a future board query

The `0030` model set contains the necessary canonical joins for the Stage 3 board read core:

| Result need | Current canonical source | Reconciliation result |
|---|---|---|
| one active authority generation | `authority_generations.status='active'` | Present, with one-active partial unique index |
| active registry | `active_section_registries` | Present |
| ordered section labels and roles | `section_registry_entries` joined to governed section/project | Present; label ambiguity still requires application validation |
| visible task | `dish_tasks.existence_state` | Present; `ordinary` and `isolated` are eligible, with isolated explicitly marked |
| incomplete task | `current_task_completion.completed=false` | Present; existing `section_tasks()` does not apply it |
| current project membership | `current_task_project_memberships.is_member=true` | Present |
| current section placement | `current_task_section_placements` | Present |
| current title/body identity | `task_authority_heads` to current activation/version | Present |
| one open operation | `workflow_operations.lifecycle='open'` | Present, with one-open-operation support |

This is enough for the checked-in set-oriented frontend board-query candidate. It is still not appropriate to serialize `PostgresReadModel.section_tasks()` or `task_view()` directly.

## Attention reconciliation

### Exact with current durable facts

- expired lease: active lease with `expires_at <= evaluation_time`, plus an explicitly persisted
  `state='expired'` only where the governing lifecycle says it remains presentation-relevant;
- open Evidence hold: `evidence_holds.state='open'`;
- awaiting human review: `human_review_requirements.state='open'`;
- active abandonment: `abandonment_attempts.state IN ('preparing','published','blocked','reconciling')`;
- active succession: a published `operation_succession_edges` row attached to the accepted active
  abandonment;
- open projection drift: `projection_drift_events.state='open'`.

### Still not exact in `0030`

- lease **invalid** and **contested** are not named states or relations. `released` and `recovered`
  are ordinary terminal states and cannot be relabelled as attention;
- Verification **failed** and **disputed** are not a closed frontend predicate. The cycle lifecycle is
  closed, but `outcome` does not by itself establish the approved product semantics;
- there is no relation literally named recovery requirement; the current Stage 3 candidate maps unresolved task-scoped `CommandExecution.status='uncertain'` with no matching `RequestUncertaintyResolution`, pending equivalence review;
- projection delayed/failed/unknown/unavailable/current/not-configured needs one accepted reducer,
  delay threshold, readiness input, and precedence.

Those gaps require either exact frontend-owned support derived transactionally from governing facts or
a targeted product-contract amendment. A query author may not fill them from phase text, free text,
operation failure alone, or browser inference.

## Required frontend support migration

These are frontend-owned support records, not additions to workflow authority.

### Stage 2 security support

Revision `0033_frontend_security` now provides the accepted implementation candidate shape:

- `frontend_security_state`: singleton current Argon2id verifier, monotonic frontend security generation,
  and hash binding to the independently current restore fence;
- `frontend_sessions`: non-recoverable token verifier, fixed issue/expiry, revocation, generation, peer
  digest, and restore-fence binding;
- `frontend_login_events`: durable bounded login outcomes used to reconstruct the fixed peer/global
  throttling windows across ordinary restart;
- `frontend_security_audit`: bounded security lifecycle/admin evidence without plaintext credentials or
  protected task content.

The restore fence itself remains outside PostgreSQL; `0033` stores only its SHA-256 binding. Native PostgreSQL,
destructive-restore/PITR, and independent Gate A evidence remain required before acceptance.

### Stage 3 read support

No Stage 3 persistence migration is currently required. The read-core candidate uses:

- stateless typed/environment-scoped HMAC-derived route identities over canonical task/section UUIDs;
- stateless opaque retry-safe cursor envelopes over canonical keyset boundaries;
- existing task-scoped command-uncertainty/resolution evidence as the candidate recovery predicate.

Secret lifetime/rotation, recovery equivalence, native query plans, and transaction-isolation/coherence
remain Gate B review items. A materialized frontend read projection remains conditional on query-plan
evidence and may never become task/workflow authority.

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
- any future frontend-owned support index justified by native plan evidence.

No index is approved solely by this list. The Stage 3 package must include representative
`EXPLAIN (ANALYZE, BUFFERS)` and fixed-query-count evidence against the final PostgreSQL schema.

## Decisions still required before production/private HTTP/browser activation

1. Which exact durable facts mean lease **invalid** and **contested**? The read core currently emits
   only durable expiry evidence.
2. Which exact Verification facts mean **failed** and **disputed**? The read core currently emits only
   durable open human-review evidence.
3. Is unresolved task-scoped command uncertainty with no resolution the accepted
   `recovery_required` predicate?
4. What is the complete projection reducer and accepted explicit delay threshold/readiness source?
5. What is the deployment lifetime/rotation policy for the route/cursor secret?
6. What exact database collation/normalization contract must match the current `lower(title), task_id`
   keyset ordering?
7. Which short PostgreSQL read transaction isolation level provides the required coherent bootstrap,
   and what native plans/bounds support it?

These decisions no longer block isolated read-core implementation or local dark-launch observation.
They do block Stage 3 production/private HTTP/browser activation where their semantics are exposed.

## Final rollout reconciliation record

Before Gate B review, fill in:

- final Alembic head:
- exact source commit/release:
- production-change ledger closed through:
- target environment schema fingerprint:
- Stage 2 frontend support migration revision: `0033_frontend_security` (implementation candidate; Gate A acceptance pending)
- Stage 3 frontend support migration revision: none currently required
- accepted activation decisions 1–7 above:
- final indexes and query-plan evidence location:
- reviewer and review date:
