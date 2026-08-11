# PostgreSQL cutover

Status: approved cutover policy; production authority has not moved to PostgreSQL.

This document is the sole product and sequencing authority for transferring Dish from the current
SQLite/Asana authority to PostgreSQL. It defines decisions, gates, and ordering. It does not contain
shell commands, implementation backlogs, schema history, or test inventories.

Marco's explicit decisions control this document. Nothing here authorizes a production mutation,
writer fence, route change, reset, rollback burn, or cutover. Exact operator commands live in the
runbooks listed under [Document routing](#document-routing).

## Current authority

- Asana owns live task content, placement, and completion.
- Service-owned SQLite owns workflow, requests, leases, recovery, authorization, and audit.
- `dish-service` is the live mutation authority; CLI, admin, frontend, and GPT Actions are transports.
- PostgreSQL is a non-authoritative import, dark-launch, and cutover target until explicit activation.
- Dark-launch execution never authorizes external effects or transfers authority.

Current ownership and transaction details live in the architecture knowledge base. This document
does not duplicate them.

## Settled cutover decisions

- Cutover is an explicit Marco decision bound to one exact candidate and evidence set.
- Cutover uses a planned write-free maintenance window.
- Legacy writers are mechanically fenced; stopping services or changing routing is insufficient.
- PostgreSQL receives one exact final import into a clean generation and an independent complete
  reconciliation before activation.
- A PostgreSQL backup must be restored into a clean database and verified before activation. An
  off-device copy is required; full PITR is not an initial requirement.
- Mutation admission is database-enforced and remains closed through activation and rollback burn.
- One exact, replay-safe PostgreSQL request is admitted and verified before general admission opens.
- After the irreversible boundary, recovery uses PostgreSQL restore or forward repair. SQLite/Asana
  cannot silently become authority again.
- Request identities remain permanently reserved.
- GPT Actions remain supported. A PostgreSQL-authoritative `create` returns canonical `dish_id`, an
  optional configured `url`, and an optional projected `asana_task_gid`; it never puts a Dish UUID
  in the legacy `task_gid` field.
- PostgreSQL commits canonical state before downstream projection. Projection failure cannot turn a
  committed command into retry advice.
- The current approved post-cutover baseline keeps Asana as a read-only projection/interface until
  Marco changes that decision. Retiring Asana completely is under consideration but is not approved.
- Transition history remains until Marco explicitly chooses its disposition after stabilization.

Workflow, Human Review, authorization, safe reclaim, abandonment, replay, and projection semantics
are owned by their architecture/runtime documents. They are cutover prerequisites, not a second
contract maintained here.

## Dark-launch baseline lifecycle

A PostgreSQL bootstrap is a point-in-time snapshot. SQLite and Asana continue changing until capture
starts, so a bootstrap performed early becomes stale even when its import was correct.

The required sequence is:

1. Rehearse preparation and reset against disposable PostgreSQL whenever useful.
2. Preserve and classify any material evidence from the current production dark-launch generation.
3. During the maintenance window immediately before enabling or resuming capture on the replacement
   baseline, run the reviewed production reset/reimport procedure.
4. Install the returned baseline identity, then enable or resume capture as a separate authorized
   operation.

Do not reset production PostgreSQL merely because a correlation fix landed when no affected evidence
exists. Do not reset early and then allow another long uncaptured interval. The reset is valuable as
the final resynchronization boundary immediately before capture, not as ritual cleanup.

The current production PostgreSQL generation is rehearsal/dark-launch evidence. It is not clean
cutover-acceptance evidence and must not be promoted as such.

## Cutover entry gates

Cutover may begin only when all applicable gates below are proven against the exact candidate.

### Runtime and contract

- The deployable PostgreSQL service, authentication, routes, clients, frontend, GPT Action schema,
  and worker release are coherent.
- Every retained command has an approved treatment and consumes shared legal-action authority.
- PostgreSQL-native `create` and its response migration are proven, unless Marco explicitly retires
  that command.
- No planning permission, discussion, finding, or proposal can satisfy mutation authorization.

### Final source and import

- Ordinary legacy mutation admission is stopped and in-flight work/effects are settled.
- Every legacy writer process, endpoint, credential, and scheduler is inventoried and fenceable.
- The exact Asana corpus and complete SQLite/WAL/sidecar authority bundle are captured.
- PostgreSQL is rebuilt as a clean generation from that source.
- Import validation finds no unknown, missing, duplicated, or mismatched entity that the accepted
  policy treats as blocking.
- Reconciliation covers the complete active corpus and exact source boundary.

### Recovery and evidence

- Backup creation, clean restore, integrity verification, and off-device retention pass.
- The writer fence is proven with an authenticated mutation rejected before body processing.
- Admission is closed and bound to the candidate generation.
- Required dark-launch/manual workflow evidence is complete, with material command-logic gaps fixed.
- Marco approves the exact candidate, source/import identities, reconciliation, fence proof,
  backup/restore result, deployed releases, and accepted discrepancies.

## Fixed authority-transfer order

The operator runbook may add checks but must not reorder these authority boundaries:

1. Enter the exclusive maintenance window and close ordinary legacy mutation admission.
2. Drain or settle in-flight requests, operations, leases, and external effects.
3. Capture the exact final Asana and SQLite authority bundle.
4. Reset/rebuild the target PostgreSQL generation and complete final reconciliation.
5. Create and verify the PostgreSQL backup/restore artifact.
6. Record candidate-bound approval and prepare the cutover run.
7. Engage and independently verify the mechanical legacy-writer fence.
8. Deploy the coherent PostgreSQL runtime while mutation admission remains closed.
9. Activate the exact PostgreSQL generation.
10. Revalidate the candidate, source closure, fence, backup, deployment, and admission state.
11. Commit rollback burn. Return to legacy authority is prohibited after this point.
12. Record post-burn runtime/worker readiness and fresh reconciliation.
13. Open admission only for the exact planned first request.
14. Issue that request once; if delivery is unknown, reconcile its exact request identity.
15. Verify its committed outcome, exact replay, audit, projection, and full reconciliation.
16. Open general admission and record cutover completion.

Routing changes alone never transfer authority.

## Abort and recovery boundaries

Before rollback burn, abort is allowed only when:

- PostgreSQL has accepted no authoritative mutation;
- no production PostgreSQL projection effect was issued;
- the legacy bundle and frozen authority remain valid;
- the writer fence can be reversed deterministically; and
- the cutover run is recorded as aborted rather than erased.

After rollback burn, do not release the legacy fence or reverse-import Asana. If the first request
fails or its delivery is uncertain, remain in maintenance, determine its commit state, repair
PostgreSQL, and replay only the same request identity when legal.

A destructive PostgreSQL restore creates a new authority generation, invalidates old capabilities
and claims, requires reconciliation and fresh registered runs, and keeps admission closed until
readiness passes. Asana observations cannot recreate erased PostgreSQL commands.

## Evidence retention

- Permanent authority facts—request-ID tombstones, generations, admission state, writer fences, and
  irreversible boundaries—are never deleted.
- Transition history remains until Marco explicitly decides otherwise.
- Cutover and stabilization reports remain through the stabilization period and a later explicit
  disposition decision.
- One-shot tools may be removed after their event closes when no runtime or recovery consumer remains;
  removing a tool never removes the durable fact it produced.

## After cutover

- PostgreSQL owns authoritative reads and mutations.
- Projection freshness is reported separately from command success and legal workflow state.
- Legacy evidence remains read-only; legacy mutation paths remain fenced.
- Monitor replay errors, stale writers, serialization/fence failures, unresolved projections,
  reconciliation drift, schema/release disagreement, and backup health.
- Remove one-shot transition tooling only after its event has closed and no consumer remains.
- Review transition-history and stabilization-evidence retention only through an explicit Marco
  decision; never through automatic expiry.

## Document routing

- Current PostgreSQL architecture: `architecture/postgresql-runtime.md`
- Dark-launch architecture: `architecture/dark-launch.md`
- Authority and durable ownership: `architecture/authority-and-data-ownership.md`
- Commands and public/private surfaces: `architecture/commands-and-surfaces.md`
- Requests and replay: `architecture/request-replay-and-idempotency.md`
- Operations, leases, and fencing: `architecture/operations-leases-and-fencing.md`
- External effects and projection: `architecture/external-effects-and-asana.md`
- Dark-launch preparation/reset/capture operations: `database-backend-dark-launch-runbook.md`
- Exact cutover, abort, and crash-recovery commands: `database-backend-stage6-runbook.md`
- Testing commands and certification policy: `testing.md`
- Settled rationale: `architecture/decisions/`

Implementation status belongs in the task tracker and Git history, not in another planning document.
