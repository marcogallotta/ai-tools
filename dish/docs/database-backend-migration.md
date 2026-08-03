# Database backend migration

Status: Stage A migration and cutover design draft

Role: this document defines how authority moves from the current Asana/SQLite system to PostgreSQL. It owns baseline capture, shadow rollout, evidence accounting, rehearsal, production cutover, rollback boundaries, and operator backup/restore procedures.

Architecture is governed by `database-backend.md`. Storage and service mechanics are governed by `database-backend-imp.md`.

## 1. Migration objective

Move authority without:

- losing current task or workflow evidence;
- allowing two live mutation authorities;
- silently importing direct Asana drift;
- replaying an external effect after its request history was erased;
- claiming command parity from reconstructed post-state;
- importing unresolved authority into production PostgreSQL;
- fabricating user commands or histories that did not occur.

The migration has three operational phases:

1. complete baseline and delta closure;
2. Asana-authoritative command shadowing and battle-hardening;
3. resolved-only production authority activation.

Production cutover is a separate Marco decision based on the recorded evidence.

The repository now contains the offline Stage 6–8 control plane through Alembic revision
`0015_verification_cycle_sequence`, `dish_pg.release`, `scripts/dish-pg-acceptance`, and
`scripts/dish-pg-release`. These components record and validate evidence, final Asana closure,
production fence proof, runtime and worker readiness, and first-admission closure, but do not claim
that a production rehearsal or cutover has occurred. The executable operator sequence, JSON inputs,
fail-closed fence order, abort boundary, and environment-only gates are defined in
`database-backend-stage6-runbook.md`. That runbook remains subordinate to this draft and cannot be
used as implicit cutover approval.

Released revisions `0003_workflow_authority` through `0007_cutover_evidence_gates` are frozen as dialect-specific DDL snapshots. They no longer import live SQLAlchemy model metadata; digest contracts and an empty SQLite downgrade/re-upgrade lane protect historical reproducibility. Real PostgreSQL upgrade and downgrade execution remains a required local certification step.

## 2. Authority timeline

### 2.1 Before shadowing

Asana and the current service-owned SQLite database remain authoritative.

PostgreSQL may contain imported test or baseline data but has no production mutation authority.

### 2.2 During shadowing

Asana/SQLite remains authoritative for live results.

PostgreSQL stores non-authoritative imported state, exact command envelopes, shadow decisions, adjudications, reconciliation observations, and proof gaps.

A PostgreSQL shadow failure never changes the live Asana result.

### 2.3 At cutover

A durable activation decision transfers production mutation authority to one PostgreSQL generation.

Before activation:

- live mutation admission is stopped;
- old writer paths are mechanically fenced;
- the final authority bundle is frozen;
- the production import is validated;
- every cutover gate is satisfied.

After activation:

- PostgreSQL is authoritative;
- Asana is downstream only;
- old Asana-authoritative service paths cannot write;
- rollback to Asana is not an ordinary option once PostgreSQL mutation admission opens.

## 3. Migration identities and evidence

Every migration artifact must have stable identity and immutable provenance.

Minimum identities:

- migration program release;
- source Dish release;
- source Honest/protocol/schema release, exact asset hashes, provenance, and operation/Verification bindings;
- legacy authority generation;
- baseline run;
- baseline high-water mark;
- delta-capture run;
- shadow rollout sequence;
- reconciliation run;
- rehearsal run;
- final legacy authority bundle;
- workflow import run;
- cutover approval;
- PostgreSQL authority generation;
- authority activation;
- projection epoch;
- restore-control generation where applicable.

Identifiers must be recorded in the relevant evidence rather than inferred from filenames or execution order.

## 4. Legacy authority generation

### 4.1 Purpose

A destructive SQLite restore can replace ordinary request, workflow, and rollout records while Asana effects and PostgreSQL shadow evidence survive.

Therefore the pre-cutover authority has an explicit legacy generation.

Bind the legacy generation to:

- service requests and client runs;
- rollout registrations and sequences;
- exact envelopes and proof gaps;
- PostgreSQL shadow deliveries and adjudications;
- baseline and reconciliation evidence;
- the final legacy authority bundle.

### 4.2 Legacy destructive restore

Before a destructive SQLite restore, restore control outside replaceable SQLite reserves a new legacy generation or a transition that deterministically establishes it.

After restore:

- requests and runs from the replaced generation are rejected or treated as permanently historical;
- prior-generation command-parity evidence is disqualified;
- surviving Asana state may be reconciled only as current-state observation;
- rollout sequences cannot be interpreted without generation identity;
- a new complete baseline and delta closure are required before command shadowing resumes.

The restored service must not infer request absence as permission to execute work from the replaced generation.

## 5. Complete legacy authority bundle

### 5.1 SQLite snapshot

Do not hash or copy only the live main SQLite file.

Produce a transactionally complete snapshot using the SQLite online-backup API or an equivalently proven checkpointed procedure that includes all committed WAL state.

Validate the snapshot with the current database schema and semantic validators.

Record:

- file identity and digest;
- source canonical database identity;
- schema version and migration history;
- snapshot method;
- capture time and authority generation;
- table counts and open-state summary;
- validation release and outcome.

### 5.2 External durable artifacts

Capture or prove clean disposition for:

- service database ownership marker;
- restore request journal;
- restore-fault marker;
- restore candidate, rollback, or checkpoint artifacts referenced by active journal state;
- invocation-audit repair JSONL;
- any active `.importing` claim file;
- managed backup records and referenced backup artifacts needed for historical proof;
- any other durable authority or recovery sidecar present at re-baseline.

The audit-repair capture must coordinate with its writer/importer lock so no append is omitted.

The lock file itself is operational coordination, not historical authority, but capture must prove exclusive coordination.

### 5.3 Completeness rule

The bundle is incomplete when:

- WAL completeness is not proven;
- any authority sidecar is unreadable or concurrently changing;
- a restore is active, ambiguous, or faulted;
- pending audit repair is neither imported nor captured exactly;
- the database identity disagrees with ownership evidence;
- referenced artifacts are missing;
- the manifest does not bind the active legacy generation and release set.

An incomplete bundle cannot be approved for rehearsal or production cutover.

## 6. Phase 0: preparation

Before baseline work:

1. freeze the current-behavior characterization corpus;
2. complete the current-to-target authority coverage matrix;
3. complete the command semantic-delta matrix;
4. identify the exact in-scope Asana project set at re-baseline;
5. identify all current durable sidecars and backup locations;
6. establish legacy authority generation handling;
7. instrument rollout registration and exact shadow envelope capture in the current authority domain;
8. instrument explicit proof-gap recording;
9. verify PostgreSQL import can be dropped and rebuilt repeatedly;
10. verify no Stage B, Cooked, Archive, or historical-lifecycle feature is an unconditional migration dependency.

No live command shadowing begins before these foundations exist.

### 6.1 Production changes from August 1, 2026 through cutover

Use the production-change ledger defined by `database-backend-imp.md` as migration evidence. Backfill it for every commit merged or deployed on or after August 1, 2026, and keep it current through durable authority activation.

For every in-scope commit:

- bind the exact source commit and release identity;
- update the current-to-target authority inventory and command semantic-delta contract when applicable;
- update baseline capture, import, delta capture, shadow envelopes, reconciliation, and acceptance evidence affected by the change;
- include schema migrations, data backfills, new tables, sidecars, commands, aliases, recovery states, and protocol changes in the final source bundle;
- re-run or invalidate prior parity and rehearsal evidence when the commit changes the behavior or durable facts those results covered.

Urgent production changes may deploy under legacy authority, but they cannot be omitted from cutover. A production feature introduced during Stage A must be preserved, explicitly retired by Marco, or explicitly isolated before activation. An unreviewed in-scope commit closes rehearsal approval and production cutover readiness.

## 7. Phase 1: baseline and delta closure

### 7.1 Baseline capture

Capture one exact baseline comprising:

- all tasks in the re-baselined in-scope Asana projects;
- all project memberships within that in-scope set;
- exact task title/body and placement observations;
- exact SQLite authority snapshot;
- all external authority/recovery sidecars;
- current release and protocol assets;
- source document identities;
- a high-water mark for ongoing legacy changes.

Do not assume the Asana task appearing first in a membership list is the governed placement. Preserve exact project/section identity.

### 7.2 Baseline import

Import into non-authoritative PostgreSQL structures with exact provenance.

For each supported current task:

- reserve one Dish UUID deterministically for the import run;
- create the task;
- create the imported title/body version;
- activate it through import provenance rather than a fabricated command;
- record all known Asana aliases and in-scope memberships;
- import the governed project/section registry, stable logical identities, project/section aliases, exact location and completion, workflow, request, execution, Verification, Honest bindings, authorization, classified lease/actor-attempt context, holds/recovery, abandonment, succession, audit, and historical evidence;
- preserve the legacy identity scheme and source occurrence.

For invalid or unsupported source evidence:

- do not invent corrected authority;
- preserve exact source evidence;
- reconcile when exact rules allow;
- otherwise isolate with a reason and provenance.

A source Asana task that is isolated does not become an ordinary authoritative task unless a later explicit reconciliation establishes valid target authority.

### 7.3 Delta closure

While baseline import runs, capture every authority change after the baseline high-water mark.

Apply deltas in exact legacy generation and sequence order until:

- the imported baseline reaches a stable high-water point;
- every registered legacy change has a delivered envelope or explicit gap;
- current Asana/SQLite observations reconcile with PostgreSQL shadow state;
- no unaccounted interval remains between baseline capture and shadow start.

Only then mark the baseline gap-free and begin command parity accounting.

## 8. Phase 2: Asana-authoritative shadow

### 8.1 Registration before effect

Before every eligible live governed effect, the current authority durably allocates a rollout sequence and registers one of:

- complete exact envelope expected to be deliverable; or
- explicit gap because exact capture could not be completed.

The registration must survive PostgreSQL outage and process restart.

A command must not disappear from rollout accounting merely because the shadow write failed.

### 8.2 Exact envelope

The envelope includes or immutably references:

- legacy authority generation;
- rollout sequence;
- request and execution identity;
- exact authenticated principal/run;
- canonical command intent and payload;
- exact authoritative pre-command snapshot;
- pinned time, UUIDs, releases, protocol, destination, and other nondeterministic inputs;
- persisted external-effect intent;
- exact live effect observations and adjudication;
- canonical live result;
- normalized authoritative after-evidence.

The before snapshot is captured before the external effect. A later reread cannot substitute for it.

### 8.3 PostgreSQL delivery

Envelope delivery is asynchronous and idempotent.

PostgreSQL records:

- received envelope identity;
- shared-planner result;
- shadow adapter result;
- adjudication comparison;
- normalized state differences;
- delivery and processing outcome;
- whether the command qualifies for parity.

Delivery failure does not alter the live result.

### 8.4 Proof gaps

A gap is permanent command-level evidence that exact parity cannot be claimed for that command.

A gap records:

- legacy generation and rollout sequence;
- request/task identity where known;
- exact failure stage;
- which required evidence is absent;
- whether current-state reconciliation remains possible;
- operational cause and repair action.

Periodic reconciliation may restore state equivalence but cannot close a command gap.

### 8.5 Current-state reconciliation

Reconciliation compares:

- current Asana corpus and placement;
- current SQLite workflow authority;
- imported PostgreSQL task and workflow projection;
- mapping and quarantine/isolation state;
- rollout sequence completeness.

It detects drift, importer defects, and missing tasks. It does not create historical command intent or parity evidence.

### 8.6 Pre-cutover create

If the current Asana API supports an atomic, discoverable, non-canonical creation marker, test exact lost-response correlation during shadow.

If it does not, pre-cutover `create` remains outside exact command-parity qualification. Its current production behavior remains unchanged until cutover.

The exclusion must be explicit in readiness statistics and cannot be hidden by current-state reconciliation.

### 8.7 Independent behavior proof

For each retained command class:

1. compare the shared planner/adjudicator with frozen current-system characterization;
2. compare live and PostgreSQL adapter results from the same exact envelope.

A command qualifies only when both comparisons pass.

## 9. Battle-hardening evidence

Battle-hardening produces an evidence package, not an automatic timer.

Track at least:

- eligible commands by route and recovery class;
- exact envelopes delivered;
- explicit gaps;
- planner versus characterization mismatches;
- live versus PostgreSQL mismatches;
- unresolved shadow processing;
- baseline and reconciliation drift;
- direct Asana edits;
- unmapped or isolated Asana objects;
- PostgreSQL outage recovery;
- legacy restore-generation events;
- projection prototype results;
- restore and backup rehearsals;
- database and service performance.

Evidence must identify feature-dependent cases separately. Deferred Cooked, Archive, Cooking History, Stage B, or broad search work cannot appear as missing Stage A coverage.

Marco decides whether the evidence is sufficient for cutover.

## 10. Rehearsal

A rehearsal uses production-shaped data and the exact intended tooling without activating production authority.

### 10.1 Rehearsal entry gates

Require:

- complete authority coverage matrix;
- gap-free baseline procedure proven;
- current-behavior characterization frozen;
- shadow envelope/gap registration proven under failure;
- PostgreSQL schema and implementation acceptance passed;
- legacy bundle capture proven, including WAL and sidecars;
- exact import repeatability;
- hard writer-fence mechanism available;
- authority activation protocol available;
- Asana projection and corpus reconciliation available;
- post-restore bootstrap authority available;
- `backup-create` and connected restore retirement represented in the target protocol.

### 10.2 Rehearsal closure

Stop or isolate mutation admission according to the rehearsal plan and prove no relevant open authority remains.

Closure covers at least:

- pending or uncertain service requests;
- executing claims or unresolved executions;
- active actor leases;
- open operations and Verification cycles;
- unresolved write or movement attempts;
- issued or claimed Planning challenges;
- active Marco authorization reservations;
- Planning reopen attempts;
- abandonment or successor transitions;
- active backup creation reservations;
- active or faulted restore state;
- pending audit repairs;
- unaccounted rollout sequences;
- unmapped visible Asana tasks.

Historical terminal rows remain evidence and do not need deletion.

### 10.3 Rehearsal bundle and import

Capture the exact legacy bundle, import into a clean PostgreSQL target, and record:

- import run identity;
- source-to-target row counts and digests;
- task/version/location identities;
- workflow semantic validation;
- request and audit preservation;
- quarantine/isolation outcomes;
- schema migration provenance;
- projection mapping initialization;
- unresolved discrepancies.

No discrepancy is waived by modifying source evidence after capture. Re-run with a new bundle when source correction is required.

### 10.4 Rehearsal activation simulation

Exercise the full activation protocol without admitting real production mutations:

- old-writer fence;
- final capture;
- import approval;
- generation creation;
- release/schema binding;
- activation;
- rollback burn;
- post-activation readiness;
- projector startup;
- stale process rejection.

Kill processes at each checkpoint and verify deterministic recovery.

### 10.5 Restore rehearsal

Exercise operator backup, restore, and PITR with:

- restore control outside PostgreSQL;
- new generation establishment;
- mutation lockout;
- schema and application validation;
- post-restore bootstrap rotation;
- stale run/request rejection;
- audit-repair reconciliation;
- projection epoch reconciliation;
- explicit deliberate reissue.

Record measured RPO and RTO. Marco accepts or rejects them later as a production gate.

## 11. Production cutover gates

Production cutover may begin only when all of the following are true.

### 11.1 Architecture and implementation

- `database-backend.md` has no unresolved Stage A human decision other than conditional Asana-create fallback if feasibility fails.
- Implementation acceptance in `database-backend-imp.md` passes.
- The normative command semantic-delta contract is complete and approved before target command implementation, and remains complete at rehearsal.
- The production-change ledger is complete from August 1, 2026 through the exact final source commit/release; every in-scope commit has an approved disposition and all affected characterization, implementation, migration, shadow, rehearsal, and acceptance evidence has been updated or rerun.
- Deferred features are non-gating.

### 11.2 Baseline and shadow

- baseline and delta closure are gap-free;
- all rollout sequences are exact envelopes or explicit gaps;
- no unexplained shadow mismatch remains;
- characterization preservation is proven;
- parity statistics are reported honestly by route;
- a legacy destructive restore after the accepted baseline has not invalidated evidence, or the baseline was rebuilt afterward.

### 11.3 Legacy closure

- no unresolved production authority remains under the cutover policy;
- no active restore/fault state exists;
- no unreadable or unaccounted sidecar exists;
- no current command can be accepted by a legacy writer after the planned fence;
- every visible in-scope Asana task is mapped for import or explicitly isolated;
- the approved final Asana snapshot covers exact content, completion, governed project membership, section placement, the active governed project/section registry, project and section identities, names, aliases and relevant registry metadata, and the complete in-scope object set;
- gap-free observation of both task state and registry/alias state remains closed through durable activation, and any relevant intervening Asana task, project, section, registry-metadata, or alias change invalidates approval and requires recapture.

### 11.4 Target readiness

- production PostgreSQL is healthy, migrated, backed up, and restorable;
- exact production import has passed validation;
The implemented release controls represent this closure explicitly. After candidate validation,
record one immutable final Asana closure containing the exact capture-manifest digest, observation
high-water mark, watcher identity, interval start, and closed-through timestamp. Marco approval binds
the closure ID and digest. Any relevant intervening change appends an immutable invalidation; the
candidate cannot activate until a replacement closure is captured and Marco records an exact
recertification. Activation names the final closure and requires its gap-free interval to include the
activation timestamp. Rollback burn revalidates the same activation-bound closure.

- authority activation can bind the approved evidence and releases;
- service and client protocol release is coordinated;
- post-restore bootstrap authority exists;
- projection readiness and corpus reconciler are healthy;
- PostgreSQL-native `create` is available through a proven safe topology; production cutover cannot leave the current governed creation semantic disabled unless Marco explicitly approves retirement.

### 11.5 Human authorization

Marco reviews the evidence package and records an explicit cutover approval bound to:

- exact final source commit/release and closed August 1, 2026 production-change ledger;
- final legacy bundle;
- final import run;
- accepted discrepancies/isolation list;
- schema and release set;
- projection readiness;
- measured backup/restore results;
- exact command coverage and proof gaps.

Approval is not a reusable blanket authorization for a later bundle or release.

## 12. Production cutover procedure

The exact commands and deployment tooling are implementation details, but the authority order is fixed.

1. Announce and begin the exclusive cutover window.
2. Stop new ordinary mutation admission.
3. Drain or settle in-flight requests and effects.
4. Establish the hard mechanical legacy-writer fence.
5. Establish an exact final Asana authority snapshot covering content, completion, governed project membership, section placement, the active governed project/section registry, project and section identities, names, aliases and relevant registry metadata, and the complete in-scope object set.
6. Maintain gap-free change closure for both task state and registry/alias state from that snapshot through durable activation; any relevant Asana task, project, section, registry-metadata, or alias change invalidates approval and requires recapture.
7. Prove legacy closure and continuous Asana corpus classification.
8. Capture the final transactionally complete SQLite and sidecar authority bundle.
9. Import into a clean production PostgreSQL target.
10. Validate semantic parity, provenance, mappings, schema, releases, and no unresolved target authority.
11. Record Marco's exact cutover approval if not already bound to the final evidence.
12. Prepare the authority activation and initial PostgreSQL generation.
13. Deploy the coherent target service, Action/OpenAPI, protocol, and routing release while mutation admission remains closed.
14. Activate PostgreSQL authority durably.
15. Commit rollback-burn evidence; once committed, return to legacy authority is prohibited even if no PostgreSQL mutation request has yet been admitted.
16. Open PostgreSQL mutation admission only after rollback burn is durable and only for the active generation and release set.
17. Start or enable downstream projection workers and corpus reconciliation.
18. Verify old direct endpoints, credentials, and processes cannot mutate.
19. Run immediate post-activation health, read, replay, mutation, projection, and stale-client probes.
20. Record cutover completion or enter the applicable recovery boundary.

Routing changes alone do not transfer authority. Credential revocation or equivalent hard fencing is mandatory, not best effort.

## 13. Authority activation recovery

Activation must be recoverable after process death at any checkpoint.

The durable evidence must distinguish:

- prepared but not active;
- active with mutation admission still closed;
- active and rollback burned;
- aborted before activation;
- failed validation requiring a new import/bundle.

A restart never guesses authority from which service happens to be reachable.

Only one activation and one PostgreSQL generation may be current.

## 14. Rollback boundary

### 14.1 Before rollback burn

Before rollback-burn evidence commits, cutover may abort back to the still-frozen legacy authority only when:

- no PostgreSQL mutation request was accepted;
- no target-side downstream effect was issued as production projection;
- the hard writer fence can be reversed deterministically;
- the legacy bundle and service state remain valid;
- the activation is recorded as aborted rather than erased.

### 14.2 After rollback burn

Once rollback-burn evidence commits, ordinary rollback to Asana is prohibited, even if no PostgreSQL mutation request has yet been admitted. Mutation admission opens only after the burn is durable.

Recovery uses:

- PostgreSQL transaction recovery;
- operator backup/PITR;
- new authority generation after destructive restore;
- projection repair from PostgreSQL authority.

Asana is never promoted back to authority through observation or reverse import.

## 15. Immediate post-cutover operation

### 15.1 Reads and mutations

All authoritative reads and writes use PostgreSQL.

The service reports projection freshness separately. Asana lag does not change legal PostgreSQL workflow actions.

### 15.2 Projection

Projection applies exact committed versions, locations, and completion state to the existing in-scope Asana projects.

Direct edits are logged and overwritten for mapped tasks. Unknown tasks are isolated or reported as blocking. They are never imported as authority.

### 15.3 Legacy system

Keep the final SQLite bundle and frozen legacy evidence read-only according to retention policy.

Legacy service mutation paths remain disabled. Historical diagnostics may read preserved evidence without gaining write authority.

### 15.4 Early-cutover monitoring

Track:

- mutation and replay errors;
- fence and serialization failures;
- projection lag and unresolved attempts;
- unknown Asana objects;
- audit repair backlog;
- schema/release disagreement;
- stale run or endpoint attempts;
- backup and WAL archive health;
- restore readiness.

## 16. Operator backup and restore

### 16.1 Backup

Backup creation is not a Dish command.

The operator procedure must define:

- PostgreSQL base backup or logical/physical backup choice;
- WAL archive and retention;
- off-host copy where required;
- encryption and access control;
- integrity validation;
- immutable artifact identity and manifest;
- schema/release/generation association;
- routine restore rehearsal.

Historical SQLite backup records remain preserved but do not control PostgreSQL backup authority.

### 16.2 Destructive restore/PITR

Restore occurs while ordinary mutation admission is closed and relevant workers are stopped or fenced.

Before replacing or rewinding PostgreSQL history, external restore control establishes:

- restore request/operation identity;
- source backup or recovery target;
- new generation transition;
- post-restore registration/bootstrap authority;
- checkpoints and terminal result;
- fail-closed marker or equivalent readiness lockout.

After database recovery:

1. validate PostgreSQL physical and semantic health;
2. apply/validate schema and application compatibility;
3. establish the new active authority generation;
4. invalidate prior run capabilities, requests, claims, fences, projection workers, and cached current-view authority;
5. reconcile audit repair and projection epochs;
6. rotate or establish post-restore bootstrap authority outside the restored timeline;
7. require fresh registered runs;
8. permit erased logical work only through deliberate reissue;
9. open mutation admission only after all readiness checks pass.

Asana observations cannot recreate erased PostgreSQL requests or commands.

## 17. Migration evidence package

The final handoff package contains:

- authority coverage matrix;
- command semantic-delta matrix;
- frozen current-behavior corpus identity;
- baseline and delta-closure manifests;
- legacy generation history;
- shadow sequence accounting;
- exact envelopes and gaps;
- parity and reconciliation reports;
- rehearsal results and fault-injection outcomes;
- final legacy bundle manifest;
- import run report;
- isolation/quarantine inventory;
- projection-create feasibility result;
- schema and migration provenance;
- backup/restore rehearsal and measured RPO/RTO;
- old-writer fence proof;
- cutover approval;
- authority activation and rollback-burn evidence;
- post-cutover validation report.

Evidence should be machine-readable where practical and linked to immutable digests.

## 18. Migration failure policy

| Failure | Required response |
|---|---|
| Incomplete baseline or delta | Do not begin or resume parity accounting. Repair capture and rebuild as necessary. |
| PostgreSQL shadow outage | Continue live Asana command after durable envelope/gap registration. Deliver later. |
| Missing exact pre-command evidence | Record permanent gap. Do not reconstruct parity from post-state. |
| Legacy SQLite destructive restore | Change legacy generation, reject old work, disqualify old parity, rebuild baseline. |
| Unreadable sidecar | Fail bundle completeness and cutover. Preserve for investigation. |
| Import semantic mismatch | Fail import approval. Correct tooling or source through a new governed process; recapture as needed. |
| Unresolved legacy authority | Drain, resolve, settle, abandon, or isolate under an explicit rule. Do not import as live open authority. |
| Hard writer fence cannot be proven | Do not activate PostgreSQL authority. |
| Asana create correlation proof fails | Keep `create` disabled during shadowing/rehearsal. Do not cut over until Marco approves a safe topology preserving current `create`, or explicitly retires it. |
| Activation crash | Recover from durable activation evidence; never infer authority from routing. |
| Failure before rollback burn | Abort only if no PostgreSQL request was admitted, no production projection was issued, and legacy authority remains valid. |
| Failure after rollback burn | Recover PostgreSQL even if no request was admitted; do not reactivate or reverse-import Asana. |
| Post-cutover Asana outage | Continue PostgreSQL authority; report projection lag and retry safely. |
| Destructive PostgreSQL restore | Establish a new generation and deliberate-reissue boundary before reopening mutations. |

## 19. Migration completion

Migration is complete when:

- PostgreSQL authority is active and healthy;
- old Asana-authoritative mutation paths are mechanically fenced;
- rollback burn is durable;
- all retained current commands and approved new commands use the coherent target protocol, while retired commands have complete preservation and retirement evidence;
- downstream projection and corpus closure are operating;
- historical source and backup/restore evidence is preserved;
- no migration tool retains hidden live mutation authority;
- the final evidence package is complete;
- post-cutover recovery uses PostgreSQL backup/restore rather than Asana authority.

Completion does not imply Stage B, Cooked, Archive, Cooking History, historical promotion, HA, or managed PostgreSQL work has begun.
