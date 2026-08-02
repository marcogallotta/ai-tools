# Database backend implementation

Status: Stage A design approved; Stages 1-5 and Stage 6's offline plumbing are implemented in code
(see §3). No real-PostgreSQL execution, backup/restore, production capture, or cutover evidence
exists yet — see "Outstanding work" below.

Role: this document tracks what remains before Stage A can go to production and defines the
acceptance bar for that remaining work. The implemented design itself (table shapes, command
semantics, domain model, principles) is no longer specified here — it lives in the shipped
`dish_pg` code and in Git history. `database-backend.md` remains the source of approved
architecture decisions; this file does not restate them.

Migration and operational cutover procedures belong in `database-backend-migration.md`.

## Outstanding work for Stage A

Nothing in this repository has run against a real PostgreSQL server. `tests/support/postgresql/core.py`
still builds `sqlite+pysqlite:///:memory:`, and `tests/conftest.py` has no `postgresql` marker, DSN
plumbing, or real-database fixture. The full certification plan is in
`database-backend-postgresql-test-plan.md`; it remains entirely open.

**Local, no production access needed:**

- Real PostgreSQL certification: run actual Alembic migrations, constraints, triggers, and
  concurrency against a real PostgreSQL instance instead of SQLite-rendered fixtures. This is the
  largest single gap.
- A full `scripts/dish-pg-acceptance` run without `--skip-full`, against the current schema head.
- Backup, restore, and PITR rehearsal against a disposable PostgreSQL instance, with measured
  (not inferred) RPO/RTO.
- Crash/fault rehearsal at each durable checkpoint listed in §2 below.
- Process-wiring verification: confirm the deployable service, projection worker, reconciliation
  worker, importer, routing, and legacy writer-fence actually exist and connect, not just that the
  control logic compiles.
- Production-shaped rehearsal (migration, activation, fault-injection, backup, restore) against
  sanitized or copied production-shaped data.

**Needs real production access:**

- Transactionally complete production SQLite + WAL + sidecar capture, with hashes.
- Production-change ledger closure from August 1, 2026 through the exact final source commit
  (`database-backend-production-change-ledger.md` must stay reconciled continuously — a later
  in-scope change can reopen already-accepted work).
- Full live Asana corpus closure, held over a gap-free observation interval through activation.
- Final production import and reconciliation into a clean PostgreSQL target.
- Production PostgreSQL readiness: migrated, backed up, restorable, monitored.
- Mechanical fencing of every legacy writer, proven by the exact authenticated `409
  CONFLICT`/`legacy_writer_fenced` response — a `401` or network failure is not proof.
- One coherent deployment of every component together (service, protocol, Action/OpenAPI,
  routing, credentials, both workers).
- First live admission: the bounded first request, verified end-to-end.

**Marco's decision, not technical work:**

- Accept or reject the measured RPO/RTO.
- Approve the exact evidence bundle (candidate, source commit, ledger closure, Asana closure,
  releases, schema head, rehearsal results).
- Authorize production activation, rollback burn, and mutation-admission opening.

Recommended order: real PostgreSQL certification → full acceptance → process/worker wiring →
backup/restore/fault rehearsal → production-shaped rehearsal → (separately) production capture,
Asana closure, import, approval, cutover.

## 1. Concurrency ceiling

Same-task contention must remain correct up to ten simultaneous agents/requests targeting one task;
independent-task work must never require global serialization to satisfy that. This is the ceiling
the outstanding contention tests (real-PostgreSQL certification, Layer 1/3 of the test plan) must
exercise for every exclusive or single-use authority (leases, challenges, authorizations,
executions, cutover admission, etc).

## 2. Transaction checkpoints for fault injection

The outstanding crash/fault rehearsal must inject failures at these boundaries (referenced by
`database-backend-postgresql-test-plan.md` Layer 3):

- before the authoritative commit of a local command (must expose none of the command bundle);
- after authoritative commit but before response (replay must return the committed outcome);
- before and after outbox claim in the projection worker;
- after durable projection intent but before the external Asana call;
- after an ambiguous Asana response but before settlement (must stay unresolved, no unsafe retry);
- during worker takeover;
- at each cutover checkpoint, before and after rollback burn;
- during database disconnect, restart, deadlock, and serialization failure.

## 3. Implementation stage status

| Stage | Purpose | Status |
| --- | --- | --- |
| 1 | Freeze source behavior and executable contracts | Done |
| 2 | Core PostgreSQL authority model | Done |
| 3 | Command execution and workflow authority | Done |
| 4 | Command and service port | Done |
| 5 | Import, shadow, and projection | Done |
| 6 | Rehearsal, acceptance, and cutover package | Offline plumbing done; everything requiring real PostgreSQL, real production access, or Marco's approval is outstanding — see "Outstanding work" above |

The actual production activation is a controlled release event, not a seventh implementation stage.

## 4. Implementation acceptance bar

This is what "done" means for the outstanding work above.

### 4.1 Schema and migration

- Fresh database migration from empty to head succeeds; upgrade through every Alembic revision succeeds.
- Applied migration provenance is immutable and complete.
- Database constraints reject duplicate task/project/section aliases, illegal registry or pointer activation, duplicate consumptions, conflicting mappings, stale fences, invalid lease classification/context, and illegal monotonic transitions.
- Restore generation changes invalidate earlier run/request authority.

### 4.2 Current-behavior preservation

- Every retained command passes its frozen characterization cases against real PostgreSQL.
- Legal actions and recovery guidance match current governing behavior unless an approved semantic-delta change says otherwise.
- Verification, completion/reopen, hold/recovery, authorization, abandonment, and successor cases pass.

### 4.3 Requests and concurrency

- Exact request replay returns the stored outcome; identity conflict fails closed; concurrent duplicate delivery performs one logical execution.
- Two-, three-, and ten-way same-task contention (§1) has exactly one winner and deterministic, side-effect-free losers, for every exclusive-authority transaction family.
- Concurrent work on unrelated tasks remains legal and is not forced through a global task lock.
- Failure injection (§2) before commit exposes nothing; after commit exposes the complete bundle and replays it correctly.
- A restored generation cannot admit old capabilities, runs, or requests; a surviving stale client cannot self-register without post-restore bootstrap authority.

### 4.4 Audit, shadow, and projection

- Governed audit and the invocation-audit obligation commit atomically with domain facts; process death after commit cannot lose or hide the obligation.
- Every registered command has an exact shadow envelope or an explicit gap; PostgreSQL outage never alters live Asana command success.
- Outbox events commit atomically with authoritative state; per-task ordering survives worker restart/takeover; duplicate delivery is idempotent; mapping cannot transfer between tasks.
- Lost create response is reconciled by exact marker before retry; multiple marker matches block automation.

### 4.5 Backup, restore, and deployment

- `backup-create` and connected restore are absent from the post-cutover command surface; historical evidence remains readable.
- Operator restore establishes an externally controlled new generation; old processes cannot regain authority automatically.
- PostgreSQL outage fails governed mutations closed; Asana outage affects projection freshness only.

### 4.6 Production-change closure

- The production-change ledger is complete through the exact source release under review, with no unreviewed or conditionally-ignored row.
- No production feature is silently dropped at cutover; it is preserved, explicitly retired by Marco, or explicitly isolated with migration evidence.

## 5. Out of scope

Do not implement without separate authorization:

- structured Stage B content;
- Cooked, Archive, Cooking History, or `log-cook`;
- general historical promotion/demotion;
- broad private search/browsing product work;
- managed PostgreSQL or HA;
- direct Asana-to-PostgreSQL ingestion;
- generic workflow unblock;
- routine task hard deletion.
