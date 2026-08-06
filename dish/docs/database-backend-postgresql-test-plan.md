# Remaining local PostgreSQL runtime validation

Status: execution plan for local evidence that cannot come from static review. The outstanding
migration work is owned elsewhere and must land before this plan runs.

## Scope

This plan covers only behavior that requires a running native PostgreSQL system or interacting
local processes. It does not request another code audit, schema review, test-architecture pass,
contract comparison, or manual repetition of an automated test.

It is subordinate to `database-backend.md`, `database-backend-imp.md`,
`database-backend-migration.md`, and `database-backend-stage6-runbook.md`. Nothing here authorizes
production access, Asana writes, activation, writer fencing, rollback burn, routing changes, or
mutation admission.

## Covered elsewhere: do not repeat

The repository already provides, and this has been run and verified — not merely written:

- an opt-in native PostgreSQL lane using an explicit test DSN and isolated ephemeral databases
  (`docs/testing.md`, "Native PostgreSQL fixture lane"): each owning test drops and recreates the
  disposable schema and runs Alembic through `head` from empty, so every native run already
  establishes the post-migration clean-database gate;
- a separate PGlite development lane, explicitly excluded from certification;
- PostgreSQL-backed Stage A semantic tests through the shared fixtures; and
- native ten-way tests for actor-lease acquisition, Marco-authorization reservation, duplicate
  request admission, and independent-task non-serialization
  (`tests/postgresql/native/test_stage_a_concurrency.py`), confirmed first-attempt-clean (no
  pass-on-rerun behavior) via rerun-detect, five random-order runs, and the mandatory
  `flake_stress` lane — see `docs/testing.md`'s recommended schedule.

This is automated evidence, already executed against real PostgreSQL. Do not recreate these writes
or assertions by hand, and do not re-run them as a manual "post-migration gate" — that gate is this
coverage. Static questions about imports, command effects, OpenAPI, release contracts, migration
source structure, and test governance remain code-review work and are outside this plan.

## Safety boundary

- Use only the disposable local PostgreSQL deployment or an equivalently isolated instance.
- Require the explicit test DSN and the repository's test-database lifecycle.
- Never target either `dish-service` profile, production PostgreSQL, the public Action route, or
  production Asana objects.
- Preserve the first failure and PostgreSQL logs; do not turn a retry into the reported result.
- Do not use PGlite or SQLite as substitute evidence for a failed native test.

## 1. Process-failure exercise

Exercise only boundaries that ordinary pytest transactions cannot prove:

- terminate the command process after authoritative commit but before its response, then verify
  exact replay from a new process;
- terminate and restart the projection worker before and after claim and after durable intent but
  before the external call;
- preserve an ambiguous external response across worker restart without an unsafe retry;
- force worker takeover and verify ownership, ordering, and durable residue;
- disconnect and restart PostgreSQL while requests and workers are active, checking the documented
  fail-closed and recovery behavior; and
- exercise deadlock and serialization-failure handling only where the application claims a defined
  policy for them.

Use deterministic process barriers or database locks, not sleeps. Do not repeat the automated
ten-way races listed above.

## 2. Backup, restore, and PITR rehearsal

Use a disposable native PostgreSQL deployment with WAL archival:

- create a backup and independently restore it;
- recover to selected transaction boundaries using PITR;
- verify authoritative content, request outcomes, audit obligations, projection state, release
  evidence, and Alembic provenance after recovery;
- prove that pre-restore runs, capabilities, leases, and workers cannot regain current authority;
- establish the required new generation and deliberate post-restore bootstrap boundary;
- measure actual RPO and RTO; and
- verify fail-closed outcomes for corrupt backup, missing WAL, interrupted restore, and unavailable
  external restore control.

The SQLite backup gate and PGlite persistence tests are not PostgreSQL recovery evidence.

## 3. Runtime wiring rehearsal

Start the deployable service and both workers against the disposable PostgreSQL target. Exercise
real process connections rather than inspecting entry points:

- prove the service, projection worker, and reconciliation worker use the intended database and
  generation;
- prove projection claim, external-attempt settlement, reconciliation, worker restart, and worker
  takeover across process boundaries;
- prove PostgreSQL loss closes governed mutation while projection freshness reflects downstream
  failure separately; and
- prove every test component is isolated from production services, credentials, routing, and Asana
  objects.

If an importer or deployment component does not yet have an executable local target, record that as
a deployment blocker rather than replacing execution with static inspection.

## 4. Production-shaped local rehearsal

After the earlier runtime gates pass, repeat migration, import, reconciliation, worker operation,
fault injection, backup, restore, and PITR against sanitized production-shaped data. Bind the
evidence to the exact source commit, schema head, PostgreSQL version, deployment identity, commands,
first-attempt statuses, output hashes, and measured durations.

This local rehearsal does not include live production capture, live Asana closure, final import,
activation, or first live admission. Those remain separately authorized production operations.

## Completion

Local runtime validation is complete only when Sections 1 through 4 have reproducible evidence. A
static pass cannot satisfy these sections, and an operator action must not duplicate an existing
automated assertion or the coverage already recorded above.

## Regression risk: script these, don't just prove them once

The historical §3 proof from 2026-08-04 is now represented by the committed
`scripts/dish-pg-runtime-wiring-rehearsal` runner. It owns a disposable TEST-only PostgreSQL
Compose instance, starts the existing service plus projection and reconciliation worker entry
points as separate OS processes, performs the fault sequence, and emits one bounded
machine-readable report. The native node proves same-logical-worker restart after process death,
different-worker takeover after claim expiry, stale-original rejection after takeover, and the
complete external-attempt lifecycle: creation, terminal state, authoritative external observation,
settlement adjudication, and no duplicate dispatch or settlement after restart or takeover. It
also proves unsupported PostgreSQL TEST routes fail closed as not-found before adapter dispatch.
A report is valid §3 evidence only when its first attempt and every explicit required-scenario
field pass. `status=blocked` is an honest native-infrastructure result, never a substitute for
native PostgreSQL and never permission to omit a scenario.

Sections §1 and §2 still need equivalent maintained runners or extension of an existing maintained
runner where the authority and lifecycle are genuinely shared. Section §4 now has
`scripts/dish-pg-production-shaped-rehearsal`; it reuses the §3 PostgreSQL TEST service path and
remains incomplete until its native run succeeds. Do not treat the §3 script as evidence for the
distinct §1 failure, §2 backup/PITR, or §4 production-shaped-data requirements.
