# Real-PostgreSQL smoke and validation plan

Status: Draft; no test execution, implementation, deployment, or cutover is authorized by this
document.

## Purpose

The Stage A package has PostgreSQL migrations and PostgreSQL-specific guards, but its current
`tests/postgresql/` database fixtures execute on SQLite. SQL rendering proves that PostgreSQL DDL can
be generated; it does not prove that PostgreSQL accepts the migrations or enforces their runtime
semantics.

This plan closes that gap in layers. The first layer is a fast, disposable smoke gate. Later layers
cover the full PostgreSQL adapter, adversarial concurrency, crash behavior, and operator recovery.
Passing an earlier layer never substitutes for a later required gate.

The plan is subordinate to `database-backend.md`, `database-backend-imp.md`,
`database-backend-migration.md`, and `database-backend-stage6-runbook.md`. It does not alter their
authority model or authorize production activation.

## Safety boundary

- Use only the isolated PostgreSQL container defined by `deploy/postgresql/compose.yaml` or an
  equivalently disposable local instance.
- Never target either running `dish-service` profile, a production database, the public Action
  route, or production Asana objects.
- Require an explicit test DSN. Do not silently fall back to the default URL or SQLite.
- Validate that destructive setup and teardown target a dedicated test database with an expected
  name prefix. Never drop a caller-supplied shared database.
- Keep test credentials, database names, and artifacts free of production secrets and data.
- Run serially first. Parallel execution is a separate diagnostic only after database isolation is
  proven.
- Do not retry a failed gate and report only the later pass. Preserve the first failure as evidence.

## Audit intake before implementation

A separate ChatGPT code audit is in progress. Reconcile its findings before fixing the test shape:

1. classify each PostgreSQL finding as a code defect, missing database invariant, missing test,
   operational gap, or non-issue;
2. add every accepted invariant and failure mode to the matrix below;
3. resolve findings that could make a test pass for the wrong reason before treating the test as
   evidence;
4. keep disagreements explicit rather than weakening assertions to match current behavior; and
5. rerun every affected layer after an accepted code or migration change.

The audit is an input, not an oracle. Its recommendations require reconciliation against the
approved architecture and migration contracts.

## Harness design

Add one durable pytest integration instead of copying database setup into each stage file.

- Register a `postgresql` marker and an explicit selector for the real-database lane.
- Accept the DSN through a test-only environment variable or pytest option. Selecting the lane
  without it must fail collection with a clear message.
- Provision a fresh database per test or per isolated test group from an administrative connection.
  Application services commit their own transactions, so outer-transaction rollback is not adequate
  isolation.
- Generate unpredictable database names beneath a fixed test-only prefix and quote identifiers
  through the driver rather than string interpolation.
- Apply Alembic migrations through the same online path used by the package. Do not use
  `Base.metadata.create_all()` as the principal PostgreSQL proof.
- Expose a SQLAlchemy session factory with the same commit ownership as `dish_pg.database`.
- Close sessions and dispose engines before dropping the database. Terminate only connections to
  the exact generated test database when cleanup requires it.
- Preserve a failed database by opt-in for diagnosis; clean successful databases automatically.
- Keep existing SQLite tests. PostgreSQL coverage is additive until a test is deliberately made
  dialect-neutral and proven on both backends.

A small launcher may own Compose startup, health waiting, pytest invocation, artifact capture, and
safe shutdown. Pytest should own test-database lifecycle; it should not start or destroy Docker
implicitly.

## Delivery order

1. Reconcile the code-audit findings and freeze the initial invariant list.
2. Add the marker, explicit DSN plumbing, isolated-database fixture, and safe launcher.
3. Add Layer 1 as a small dedicated test module; do not first parameterize all 51 existing tests.
4. Run Layer 1 once, preserve its first-attempt result, and classify every failure before fixing it.
5. After Layer 1 is trustworthy, convert applicable existing tests into the Layer 2 lane in bounded
   groups by stage.
6. Add deterministic race and fault fixtures for Layer 3 only after ordinary PostgreSQL behavior is
   green.
7. Update `docs/testing.md` and `scripts/dish-pg-acceptance` when the new lane is stable enough to
   become a blocking repository gate.
8. Design and execute Layer 4 separately with its own operator procedure and evidence location.

The first implementation handoff should contain the harness, Layer 1 tests, first-attempt output,
and any diagnosed defects. It should not claim Layers 2 through 5 are complete.

## Layer 1: disposable smoke gate

This is the first pass. It answers whether the implemented target can start and whether its most
important PostgreSQL-only protections execute.

### Bootstrap and migration

- Start PostgreSQL 17.5 and wait for `pg_isready` plus a successful SQL connection.
- Upgrade one truly empty database from Alembic base to `0005_release_cutover`.
- Confirm the exact Alembic head and expected Stage 2 through Stage 6 tables.
- Confirm the expected PL/pgSQL functions, triggers, partial unique indexes, foreign keys, and check
  constraints exist in PostgreSQL catalogs.

### Representative runtime guards

Exercise at least one real rejection from each PostgreSQL migration layer:

| Layer   | Required proof                                                                                                                           |
| ------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Stage 2 | Immutable authority evidence rejects update and delete; an illegal generation or current-pointer transition is rejected.                 |
| Stage 3 | Stale run/generation admission is rejected; immutable workflow evidence is protected.                                                    |
| Stage 5 | An outbox event for the wrong or inactive epoch is rejected; mapping identity cannot transfer between entities.                          |
| Stage 6 | Immutable release evidence is protected; an illegal candidate/cutover revision is rejected; closed mutation admission rejects a request. |

Assertions must check PostgreSQL SQLSTATE or named constraint/trigger identity where stable, not only
generic `IntegrityError` text.

### Representative contention

- Ten simultaneous actor-lease acquisitions for one task produce exactly one committed winner and
  nine typed losers, with one active lease afterward.
- Ten simultaneous reservations of one Marco authorization produce exactly one committed winner,
  one reservation event, and no duplicate consumption authority.
- Two independent tasks can acquire their own authority without a shared global fence.
- A request racing closed Stage 6 admission cannot commit after closure becomes authoritative.

The smoke gate passes only if all checks succeed on their first attempt and teardown confirms it did
not contact a live Dish or Asana endpoint.

## Layer 2: real-PostgreSQL regression gate

After smoke passes, run every database-semantic test whose behavior is intended for the target
backend against PostgreSQL. Do not mechanically port SQLite inventory, PRAGMA, WAL, locking-error,
or historical-SQLite migration assertions.

Coverage must include:

- atomic import activation and rollback of incomplete authority bundles;
- exact request replay, identity conflict, execution claims, and stored outcomes;
- task and operation fences, Planning challenges, Verification evidence, holds, abandonment,
  invocation-audit obligations, and Marco authorization lifecycle;
- retained Stage 4 command and read behavior using the frozen characterization oracle;
- atomic command authority plus projection-outbox publication;
- source import, shadow delivery, gap closure, projection ordering, idempotency, claim/takeover,
  uncertain-effect settlement, reconciliation, drift, and create correlation;
- release-candidate evaluation, deterministic bundles, stale-bundle rejection, writer fencing,
  cutover checkpoints, admission control, and resumability; and
- PostgreSQL-native timestamps, UUIDs, JSON values, partial indexes, foreign keys, and transaction
  visibility at every repository boundary.

Tests that intentionally exercise SQLite remain in their existing lanes and are not counted as
PostgreSQL evidence.

## Layer 3: adversarial concurrency and fault gate

Exercise the Stage A operating envelope at two-, three-, and ten-way same-task contention. Cover
each singleton authority: request admission, execution claim, open operation, actor lease, Planning
challenge claim, authorization reservation/consumption, Verification cycle/signoff, hold,
abandonment, projection claim, mapping creation, and cutover transition.

For each race, assert both the result and the durable residue: one winner, typed loser outcomes,
complete rollback of losing writes, no orphaned rows, no duplicate audit/outbox facts, and no
unresolved transaction.

Inject failures at the transaction contracts defined in `database-backend-imp.md`:

- before authoritative commit;
- after authoritative commit but before response;
- before and after outbox claim;
- after durable projection intent but before external response;
- after ambiguous response but before settlement;
- during worker takeover;
- at each cutover checkpoint before and after rollback burn; and
- during database disconnect, restart, deadlock, and serialization failure.

Use deterministic barriers or database locks, not sleeps. Define the expected retryable SQLSTATEs
and maximum application retry policy before testing them. Unexpected deadlocks, connection errors,
or timeouts fail the gate.

## Layer 4: operational backup, restore, and PITR rehearsal

This is not part of the fast smoke gate and cannot be proven by unit fixtures. Use a
production-shaped but non-production PostgreSQL deployment and the external restore control required
by the migration design.

- Establish backup and WAL archival, then independently verify recoverability.
- Restore the latest backup and perform point-in-time recovery to selected transaction boundaries.
- Prove that pre-restore runs, capabilities, leases, and workers cannot resume as current authority.
- Establish a new authority generation and deliberate post-restore bootstrap/reissue boundary.
- Verify authoritative content, workflow, request outcomes, audit obligations, projection state,
  release evidence, and migration provenance after recovery.
- Measure actual RPO and RTO; record observations rather than targets.
- Repeat with backup corruption, missing WAL, interrupted restore, and unavailable external restore
  control, all of which must fail closed.

The existing SQLite backup/restore gate remains valid for the legacy system but is not evidence for
PostgreSQL recovery.

## Layer 5: production-shaped pre-cutover acceptance

Run only after the earlier layers pass and the production-change ledger is closed through the exact
candidate commit. This layer combines, without collapsing, the required evidence:

- clean-schema migration and the full real-PostgreSQL regression gate;
- frozen current-behavior and independent live-versus-target shadow comparison;
- complete import, delta closure, projection mapping, and corpus reconciliation;
- ten-way contention and crash-boundary results;
- lost-response-safe Asana create feasibility using non-production objects;
- backup, restore, PITR, generation invalidation, and measured RPO/RTO;
- coherent service, protocol, OpenAPI, routing, credential, and writer-fence deployment; and
- full repository smoke, database-boundary, and complete-suite gates.

Artifacts must bind the source manifest, commit, releases, schema head, database version, Compose or
deployment identity, test commands, first-attempt exit status, output hashes, and measured duration.
Record a new evidence revision after any relevant source, migration, dependency, PostgreSQL image,
or audit disposition changes.

## Failure handling

On any failure:

1. preserve the first-attempt node ID, SQLSTATE, constraint or trigger, transaction participants,
   PostgreSQL logs, and durable rows;
2. distinguish harness defects from implementation defects before changing code;
3. reproduce once with the smallest equivalent test only for diagnosis, never to erase the failed
   gate;
4. do not broaden accepted errors, add timing sleeps, disable triggers, or substitute SQLite; and
5. rerun the failed layer from a clean database after the cause is fixed, then rerun all downstream
   layers whose evidence it invalidated.

## Completion criteria

The coverage gap is closed only when Layers 1 through 3 are durable, blocking, and reproducible on
real PostgreSQL. Layer 4 must also pass before PostgreSQL production readiness can be claimed.
Layer 5 and the exact approvals in the Stage 6 runbook remain mandatory before cutover.

No result from this plan authorizes production administration, candidate approval, writer fencing,
rollback burn, route changes, or mutation admission.
