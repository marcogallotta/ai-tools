# PostgreSQL runtime

## Read this when

Read this when changing PostgreSQL authority, models, command execution, replay, workers, reconciliation, migrations, release evidence, or cutover controls.

## Scope

This document describes the PostgreSQL replacement architecture and current migration boundary. It does not treat today's module decomposition as permanent.

## Authoritative implementation

Current anchors include PostgreSQL models/migrations under `dish_pg/`, `dish_pg/command_port.py`, `dish_pg/postgres_service.py`, `dish_pg/transition.py`, workers, and release/cutover services. The current schema head is whatever `dish_pg/release.py` (`ALEMBIC_HEAD`) currently names; `0032_imported_operation_history.py` remains the current task/workflow-history migration anchor.

## Actors, processes, and stores

The PostgreSQL target contains command/replay/workflow/projection/reconciliation/release state. Before cutover it is non-authoritative for production mutations; after explicit cutover it becomes canonical backend authority.

## Authority and data ownership

Authority transfer is explicit and one-way for the activated generation. Imported/shadow evidence before activation does not become production authority merely by existing in PostgreSQL.

## Invariants

- Request admission/outcome semantics preserve permanent request identity.
- Validation-only failures are recorded through the target replay authority (`record_replay_validation_failure` / `record_validation_failure`) where applicable.
- The first-request reservation and activation/admission controls prevent uncontrolled authority opening.
- Projection origin/effect settlement remain separate from canonical command authority.
- Reconciliation is evidence/repair machinery, not an alternate canonical writer.
- Forward candidate-authority manifests use contract v3: they bind the exact approval-time reconciliation run and exclude all post-burn worker-readiness state. Historical v2 fingerprints keep their original stored semantics.
- Supplemental terminal-history application and candidate validation serialize on the active generation. Primary-only v3 manifest fingerprints retain their original bytes; when supplemental terminal history exists, the v3 builder extension folds a deterministic digest of supplemental ImportRun/source/primary linkage and exact imported terminal operations, verification cycles, and leases into `import_completion_sha256`.
- Post-burn projection-worker readiness is one immutable `projection_worker_readiness` report with validator-owned `claim`, `exact_write`, and `restart` probes, exact worker/artifact identity, exact fresh reconciliation identity, and a report SHA-256 revalidated at first admission.
- PostgreSQL-backed service promotion is schema-gated: for each explicit TEST or PROD target, the exact release/source commit and repository `ALEMBIC_HEAD` must be bound to durable migration evidence, and that target must be re-read at the exact expected head before its corresponding service is restarted/promoted. TEST evidence never proves PROD. Migration failure blocks promotion; startup validation remains fail closed and never performs DDL.

## Process and transaction boundaries

PostgreSQL uses SQLAlchemy sessions/transactions, row locks, revisions, claims, and generation/epoch fences. Current services often participate in caller-managed transactions; that is current design, not a permanent product rule.

## Normal flow

Admit request, execute canonical mutation, record outcome/audit/projection intent, project/observe separately, reconcile drift, and expose release/cutover evidence when preparing authority transfer.

## Failure, replay, recovery, and concurrency

Runtime recovery relies on durable request/claim/effect identities. Reconciliation is coordinated by `start_reconciliation`, `record_reconciliation_item`, and `complete_reconciliation` paths. Native PostgreSQL is required to certify behavior that depends specifically on PostgreSQL locks/DDL/process semantics; other tests can still provide useful non-final evidence.

Destructive-recovery authorization separates immutable historical authority from current recovery health. Historical proof uses the exact generation/candidate, CutoverApproval and evidence bundle, approval-to-manifest binding, source/import lineage, and activation/rollback-burn evidence where applicable. Recovery does not require mutable mapping, reconciliation, readiness, or import-linkage corpora to reproduce their approval-time manifest values merely to prove that historical authority transition. Physical restore identity, current schema/release fencing, active-generation identity, registry viability, and other operation-specific recovery checks still fail closed against the recovered state as it exists now.

## Change routing

Keep canonical mutation authority, projection/effect settlement, and operational release controls conceptually distinct even if module boundaries evolve. Avoid building a second workflow/replay implementation merely to satisfy a worker or transport.

## Proving tests

PostgreSQL test commands and certification policy live in `docs/testing.md`; unresolved operational
findings live in the task tracker or `docs/ops-issues.md`. Historical passing reports are not
evidence that a current rehearsal still passes after relevant code changes.

## Current debt and temporary compatibility

Legacy and PostgreSQL implementations overlap during migration. The migration chain, release tooling, and compatibility adapters may be simplified after retained invariants/data are known. Exact current modules/tables are not themselves architecture commitments.

## Related documents

- [Dark launch](dark-launch.md)
- [Routine release migration gate](../postgresql-routine-migration.md)
- [Request replay and idempotency](request-replay-and-idempotency.md)
- [External effects and Asana](external-effects-and-asana.md)
