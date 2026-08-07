# Dish testing and flaky-test operations

This is the operational runbook for Dish tests. It defines the authoritative gates, the separate
flake-detection environment, the evidence required before calling a test flaky, and the temporary
quarantine rules.

## Test environments

Use two repository-local environments. Do not package either environment.

### Deterministic development environment

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
```

This environment runs the authoritative first-attempt gates. It deliberately does not install
plugins that randomize order or rerun failures automatically.

### Flake-detection environment

```sh
python3 -m venv .venv-flake
.venv-flake/bin/python -m pip install -r requirements-flake.txt
```

This environment adds `pytest-rerunfailures`, `pytest-randomly`, `pytest-repeat`, and
`pytest-xdist`. Use it only through the explicit commands below. `pytest-randomly` changes normal
pytest behavior when installed, which is why it is kept out of `requirements-test.txt`.

## Autonomous changed-path selection

For every Dish code or test change, start with the complete changed-path set:

```sh
# All changes from a branch base through the current working tree.
.venv/bin/python scripts/dish-test-plan --base <revision>

# Or an explicit path set while iterating.
.venv/bin/python scripts/dish-test-plan \
  --path dish_tool/example.py \
  --path tests/test_example.py
```

The command reads `test_selection/ownership.csv`, takes the union across mixed changes, and prints
focused owner tests plus governed lane commands. For Git-based planning it also reads the map at the
base revision so deleted paths retain their prior test ownership. The map is a strong current-HEAD
prior; it does not replace semantic review. An agent must evaluate the actual invariant, authority, durable state,
external effect, transaction boundary, and release consequence changed. Add any additional required
lane explicitly:

```sh
.venv/bin/python scripts/dish-test-plan \
  --path dish_tool/example.py \
  --add-lane 'SQLite database-boundary'
```

The eight primary classes are:

| Class | Scope |
| --- | --- |
| 1 | Documentation and isolated tests; test rows also require `domain_class_for_tests` |
| 2 | Frontend |
| 3 | Ordinary Python/service logic |
| 4 | Authority and canonical identity |
| 5 | Recovery and filesystem behavior |
| 6 | Schema, ORM, and migrations |
| 7 | PostgreSQL concurrency and projection lifecycle |
| 8 | Release, cutover, dark launch, and import |

Each path has one primary class plus a bounded set of escalation traits. Mixed changes always take
the union. New in-scope paths must be classified in the map in the same change. Agents decide the
classification for new architecture and ask Marco only when the owning authority or acceptable
evidence remains materially ambiguous.

Normal scoped work runs:

1. `direct_owner_tests` and `critical_contract_tests`;
2. the row's `default_lanes`;
3. consumer lanes required by bounded shared-test fan-out;
4. every additional lane triggered by the actual semantic delta.

Test rows select their own module automatically, so `direct_owner_tests` and
`critical_contract_tests` only record additional cross-file evidence. Shared test infrastructure
uses fan-out scope: narrow helpers run known consumers; cross-lane helpers run their consumer lanes;
only genuinely global collection, dependency, fixture, selector, or governed-runner changes force
the ordinary full suite before handoff. A row addition that only classifies a new path does not by
itself force the full suite.

Validate the map after adding, deleting, renaming, or reclassifying a path:

```sh
.venv/bin/python scripts/dish-test-plan --validate
```

After a successful fresh collection, strengthen validation with:

```sh
.venv/bin/python -m pytest --collect-only -q \
  > .test-artifacts/collected-nodeids.txt
.venv/bin/python scripts/dish-test-plan --validate \
  --collected-nodeids .test-artifacts/collected-nodeids.txt
```

Every handoff records the chosen class and traits, the changed invariants, commands and first-attempt
results, conditional lanes omitted with reasons, and any unresolved uncertainty. Intermittent audits
review these decisions; repeated mistakes should become clearer map rules, examples, inventories, or
structural checks.

## Named lane commands

Use the single lane entrypoint when the change belongs to one of the recurring high-risk groups.
Each command prints `BEGIN`, `PASS`, `FAIL`, or `UNAVAILABLE` for the exact phase that stopped the
lane; it never hides a failed inner phase behind one final aggregate result.

| Lane | Command |
|---|---|
| schema and migrations | `.venv/bin/python scripts/dish-test-lane schema-migrations` |
| PGlite | `.venv/bin/python scripts/dish-test-lane pglite` |
| native PostgreSQL concurrency | `.venv/bin/python scripts/dish-test-lane native-concurrency` |
| release and cutover | `.venv/bin/python scripts/dish-test-lane release-cutover` |
| command and API contracts | `.venv/bin/python scripts/dish-test-lane command-api-contracts` |
| operational certification | `.venv/bin/python scripts/dish-test-lane operational-certification` |

`native-concurrency` requires `DISH_TEST_POSTGRESQL_DSN`; `operational-certification` requires
`DISH_PG_TEST_URL`. Missing infrastructure is reported as unavailable with exit status 3, never as a
pass. These commands complement, rather than replace, changed-path focused tests and the ordinary
full-suite integration checkpoint.

## Authoritative first-attempt lanes

The planner may emit any of these separately reported lanes:

```sh
.venv/bin/python -m pytest --smoke
.venv/bin/python -m pytest --database-boundary
.venv/bin/python -m pytest
```

The ordinary full suite is mandatory at concrete integration checkpoints, not after every scoped
edit:

- before merge or integration of a completed change block;
- before a final staged archive;
- after conflict resolution affecting shared code;
- after global selector, fixture, dependency, marker, or runner-policy changes;
- before release or cutover certification.

Authoritative first attempts never rerun failures automatically. Preserve and report the first
result. One lane-level retry is allowed only for a narrowly proven infrastructure signature such as
connection refusal, unavailable native PostgreSQL, PGlite process startup failure, unexpected server
closure, connection reset, or broken pipe before an assertion or domain transaction executes. Use
the exact same commit, environment, selection, and command, and report both attempts. Never retry an
assertion, SQL constraint, migration, domain-rule, lock-order, state, hash, collection, or inventory
failure. An ambiguous timeout is a correctness failure until infrastructure failure is proved.

### Source-contract acceptance

`scripts/dish-pg-acceptance` is source-contract acceptance. Its report now names
`acceptance_scope=source_contract` and always reports `native_postgresql_certified=false`.
A source-contract pass is not native PostgreSQL certification and is not production operational
rehearsal evidence.

### Mandatory native PostgreSQL certification lane

Run the governed native inventory with:

```sh
DISH_TEST_POSTGRESQL_DSN='postgresql+psycopg://...' \
  .venv/bin/python scripts/dish-pg-native-certification \
  --output .test-artifacts/native-postgresql/report.json
```

The script probes the target before pytest, rejects SQLite and PGlite server identities, invokes
pytest with both `--postgresql` and `--native-postgresql`, and compares collection with the literal
inventory in `tests/support/postgresql/certification.py`. Certification fails when zero tests execute,
when inventory identities drift, when setup errors occur, or when a required test skips without an
explicit `--waive-skip NODEID=REASON`. The report includes dialect, driver, database, native server
version, selected/executed/passed/failed/error/skipped/unavailable counts, duration, and exact node
IDs.

Native-marked tests in ordinary source or full-suite runs skip before their bodies with a governed
reason unless `--postgresql` is present. They never substitute SQLite. The native branch of
`tests/support/postgresql/core.py` drops and recreates the disposable `public` schema before each
owning test, then runs Alembic through `head`. It must not use `Base.metadata.create_all()`:
hand-written PostgreSQL triggers and constraints are part of the behavior under certification.

### §1 process-failure rehearsal (Compose-driven, not part of native certification)

Native certification's inventory (previous section) is auto-discovered: it globs every
`tests/postgresql/native/test_*.py` file with no filtering for Compose dependency. A subset of that
inventory needs live control over the PostgreSQL server process itself (stop/start/disconnect) and
therefore FAILS — it does not skip — when collected and run standalone without a Compose-controlled
target. Do not treat those failures as native-certification defects; run the dedicated rehearsal
instead:

```sh
.venv/bin/python scripts/dish-pg-process-failure \
  --output .test-artifacts/process-failure/report.json \
  --evidence-dir .test-artifacts/process-failure/evidence
```

This spins up its own disposable Docker Compose PostgreSQL stack (own port, own database, own
lifecycle) and runs a fixed, literal 14-node inventory (`PROCESS_TEST_INVENTORY` in
`dish_pg/process_failure_rehearsal.py`) covering §1 process-failure command handling, projection,
takeover, supervision, reconciliation, and disconnect scenarios. It deliberately invokes pytest
with `--postgresql` but not `--native-postgresql`, so it never triggers the governed
full-repository-collection rule for native lanes.

**Known gap:** `test_section4_service_database_disconnect_rolls_back_then_recovers_once`
(`tests/postgresql/native/test_production_shaped_runtime.py`) also calls `compose_control()`
against the live TEST PostgreSQL target, but it is in neither this rehearsal's fixed inventory nor
any other runner's Compose wiring. It currently has no runner that passes cleanly. Tracked
separately; do not assume `dish-pg-process-failure` covers it.

### Local PostgreSQL 17 server binaries (backup/PITR and production-shaped rehearsals)

`scripts/dish-pg-recovery-rehearsal` (test-plan §2) and `scripts/dish-pg-production-shaped-rehearsal`
(§4) drive `initdb`, `pg_ctl`, `postgres`, `createdb`, `pg_basebackup`, `pg_verifybackup`, and
`pg_controldata` directly against a local disposable data directory via `--pg-bin`; a Docker-only
PostgreSQL is not sufficient for these two sections. The host needs real PostgreSQL 17 server
binaries installed, e.g. from the PGDG apt repository:

```sh
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
sudo sh -c 'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
sudo apt update
sudo apt install postgresql-17
```

This installs binaries under `/usr/lib/postgresql/17/bin` (pass via `--pg-bin` if `discover_pg_bin()`
does not find them) and also creates a system-wide PG17 cluster on port 5432 via
`postgresql-common`; drop it with `sudo pg_dropcluster 17 main --stop` if you don't want a
background instance running. Version 17.10 matches the `postgres:17.10` image used by the §1/§3 Compose
rehearsals, so recovery evidence stays on the same major version across all four sections.

### PGlite development lane

Run PGlite separately with:

```sh
.venv/bin/python scripts/dish-pg-pglite \
  --output .test-artifacts/pglite/report.json
```

The report is explicitly non-certifying. It first collects each governed inventory from the complete
repository, then runs every selected node in its own fresh pytest process. Each node has a hard
timeout, file-backed stdout/stderr and JUnit evidence, and forced cleanup of its pytest process group
plus any detached Node/PGlite descendants recorded while it ran. The runner prints `BEGIN`, `PASS`,
`FAIL`, or `TIMEOUT` for each exact node and writes the per-node artifacts beside the JSON report.
This isolation is runner-only: manual lane selectors combined with explicit test paths remain
prohibited, and the ordinary pytest suite excludes the separately governed PGlite inventory. The
normal PGlite inventory and foundational quarantine inventory remain separate; the
runner refuses to let the quarantined lifecycle test disappear silently and classifies known
connection-lifecycle failures separately from assertion or schema failures. PGlite success never
sets native PostgreSQL certification true.

## Flaky-test classifications

### Normal

The test has no confirmed nondeterminism. It runs in every applicable blocking gate.

### Flake candidate

The test has failed unexpectedly, but the same code and materially equivalent environment have not
yet produced both a pass and a failure.

A candidate remains blocking and may be marked only with complete investigation metadata:

```python
@pytest.mark.flake_candidate(
    issue="DISH-123",
    owner="Marco",
    first_seen="2026-07-31",
    signature="legacy backup schema version mismatch during teardown",
)
def test_example():
    ...
```

Run candidates directly with:

```sh
.venv/bin/python -m pytest --flake-candidates
```

No tests are currently marked as flake candidates. The previously investigated concurrent
planning-intent failure was traced to a real database-initialization race: the legacy schema version
and online-backup source could come from different SQLite snapshots. The backup now keeps both on
one read transaction, and the formerly quarantined test is back in the normal blocking suite.

### Quarantined flake

Quarantine is permitted only after the same commit and materially equivalent environment have
produced both a pass and a failure. A quarantined test is removed temporarily from normal gates but
continues to run in a separate mandatory lane.

```python
@pytest.mark.quarantined(
    issue="DISH-123",
    owner="Marco",
    first_seen="2026-07-29",
    quarantined_on="2026-07-31",
    expires="2026-08-07",
    signature="legacy backup schema version mismatch during teardown",
)
def test_example():
    ...
```

Quarantine rules are enforced during collection:

- issue, owner, first-seen date, quarantine date, expiry, and failure signature are mandatory;
- dates use `YYYY-MM-DD`;
- quarantine lasts at most seven days;
- expired quarantine fails collection;
- launch-critical smoke or invariant tests also require an explicit `waiver` field;
- a test cannot be both `flake_candidate` and `quarantined`;
- automatic per-test `@pytest.mark.flaky(reruns=...)` is forbidden.

Two PGlite lifecycle tests are currently quarantined and remain visible through the separate PGlite quarantine result. They do not certify native PostgreSQL behavior.

## Reproducible detection commands

Run commands from `dish/`. Each command creates a unique directory below
`.test-artifacts/flakes/` containing JUnit XML, seeds, command lines, environment metadata,
requirement-file hashes, and a machine-readable summary.

### Detect pass-on-rerun behavior

```sh
.venv-flake/bin/python -m tests.flake_runner rerun-detect
```

This runs pytest with two reruns and `--fail-on-flaky`. The command fails when a test fails first
and passes later. It is diagnostic; it never replaces the normal first-attempt gates.

Narrow it when investigating one area:

```sh
.venv-flake/bin/python -m tests.flake_runner rerun-detect -- \
  tests/test_planning_intent_confirmation.py
```

### Randomize test order

```sh
.venv-flake/bin/python -m tests.flake_runner random-order --runs 5
```

Every run uses a fresh process and a recorded seed. Reproduce one failure with:

```sh
.venv-flake/bin/python -m pytest \
  --randomly-seed=<recorded-seed> \
  --randomly-dont-reset-seed
```

The runner randomizes collection order but disables pytest-randomly's per-test reseeding. Some
installed libraries register seed hooks that accept only unsigned 32-bit values; derived per-test
seeds can exceed that range even when the recorded order seed is valid. Disabling the reset keeps
the diagnostic signal focused on order dependence and preserves exact seed reproducibility.

Explicit seeds can be supplied when repeating a known investigation:

```sh
.venv-flake/bin/python -m tests.flake_runner random-order \
  --runs 2 --seed 1234 --seed 5678
```

### Stress deterministic concurrency and recovery tests

Tests marked `flake_stress` represent high-risk concurrency, recovery, shutdown, and audit races.
Run them twenty times in fresh processes with recorded order seeds:

```sh
.venv-flake/bin/python -m tests.flake_runner stress --runs 20
```

The marker is diagnostic selection, not a flaky label.

### Investigate one suspected test

```sh
.venv-flake/bin/python -m tests.flake_runner repeat \
  'tests/test_file.py::test_name' \
  --same-process 50 \
  --fresh-runs 30
```

The same-process phase is a fast screen. Fresh-process repetitions are the stronger evidence because
they reset module globals, thread state, SQLite connections, environment changes, and pytest caches.

Also compare these contexts manually when needed:

```sh
# Exact node only
.venv/bin/python -m pytest 'tests/test_file.py::test_name'

# Containing file
.venv/bin/python -m pytest tests/test_file.py

# Recorded full-suite order
.venv/bin/python -m pytest

# Candidate-only lane
.venv/bin/python -m pytest --flake-candidates
```

### Parallel stress

Parallel execution is a diagnostic lane, not the primary debugging environment:

```sh
.venv-flake/bin/python -m tests.flake_runner parallel --workers 2 --workers 4
```

Once a failure appears, reproduce serially unless parallelism itself is required for the bug.

### Quarantine lane

```sh
.venv-flake/bin/python -m tests.flake_runner quarantine --runs 20
```

The command exits successfully when no tests are quarantined. Quarantined tests are repeated in
fresh randomized processes and remain visible in the artifact summary.

### Static risk inventory

```sh
.venv/bin/python -m tests.flake_runner scan
```

The scan reports common risk signals such as real sleeps, threads, subprocesses, wall-clock calls,
randomness, environment mutation, and working-directory mutation. A finding is a review candidate,
not proof that a test is flaky.

## Failure triage

For every unexpected failure record:

- full pytest node ID;
- commit SHA;
- Python version and platform;
- `requirements-test.txt` and `requirements-flake.txt` hashes;
- random seed and xdist worker count, when applicable;
- whether it passes alone;
- whether it passes in its containing file;
- repeated same-process and fresh-process outcomes;
- normalized exception and traceback signature;
- JUnit XML and the flake-runner `summary.json`.

Interpret repeated outcomes as follows:

| Alone | File | Full/randomized suite | Likely class |
| --- | --- | --- | --- |
| intermittently fails | intermittently fails | intermittently fails | intrinsic timing, randomness, or resource race |
| passes | sometimes fails | sometimes fails | file-level fixture or shared-state leak |
| passes | passes | sometimes fails | cross-module order pollution |
| passes serially | fails under xdist | fails under xdist | parallel resource or locking race |
| always fails | always fails | always fails | deterministic defect, not flaky |

## Prohibited “flake fixes”

Agents must not make a failure disappear by:

- adding or increasing arbitrary sleeps;
- increasing a timeout without proving resource insufficiency;
- broadening accepted error rules;
- weakening or deleting assertions;
- catching a broader exception;
- skipping or deleting the test;
- applying automatic reruns to the test;
- marking a test flaky after one failure;
- extending quarantine without a new explicit decision.

Preferred fixes use deterministic barriers, events, fake clocks, isolated resources, exact producer
state, complete thread/process teardown, and independent oracles.

## Recommended schedule

| When | Required work |
| --- | --- |
| Every scoped code handoff | planner-selected focused tests and governed lanes, with classification and omission reasons |
| Integration, merge, or final staged archive | planner-selected lanes plus the ordinary full suite |
| Global test infrastructure or selector change | all affected governed lanes, ordinary full suite, and structural map validation |
| Test infrastructure or concurrency change | rerun detector, five random-order runs, and twenty `flake_stress` runs |
| One unexpected failure | exact-node repeat workflow and recorded triage evidence |
| Nightly or periodic health check | rerun detector, random-order, stress, parallel diagnostic, quarantine lane, and static scan |
| Before release or cutover certification | ordinary full suite plus every required native, PGlite, migration, mutation, and acceptance lane |

## Artifact retention

`.test-artifacts/` is ignored by Git. Preserve the relevant run directory with the associated issue
or handoff whenever it contains a failure. Do not commit generated JUnit XML or local environment
metadata.


## Test module structure

Collected test modules must not import helpers from another collected test module. Shared
PostgreSQL builders and fixtures live under `tests/support/postgresql/`. Import and size governance
walk the complete `tests/` tree, including `tests/postgresql/`; split a file by stable behavior
ownership before it exceeds the repository ceilings.

## Generated artifact assurance

A checked-in generated artifact needs two different tests:

1. a synchronization test proving the checked-in bytes or semantic snapshot match a fresh generator run;
2. an independent contract test whose expected inventory, states, and invariants are not imported from the generator or production constants.

The recovery fixture database and its JSON sidecars are checked both ways. Updating the generator and
checked-in artifacts together is insufficient unless the independent literal contract is also reviewed.

## Curated mutation sample

Run the launch-critical mutation sample with:

```sh
.venv/bin/python -m tests.mutation_runner
```

Run the bounded PostgreSQL Stage A safety lane independently with:

```sh
.venv/bin/python -m tests.mutation_runner --stage-a
```

The Stage A lane is intentionally small. It probes strict external-evidence hashing, mandatory
transactional projection authority, authenticated writer-fence proof, and the explicitly scoped
command-effect verification contract. Projection effects are checked for every command; mutation
observation is currently pinned only for `prepare`, `approve`, and `reject`. Add a mutant only when
it represents a realistic release-safety regression and one
focused authoritative test kills it.

The runner mutates request replay, request/run binding, lease ownership, governed authorization
consumption, verifier independence, terminal cancellation evidence, Planning intent dimensions,
workflow-policy fail-closed facts, Cooking-project membership selection, and post-mutation Asana
confirmation in isolated temporary copies. A mutant is counted as killed only when its targeted
pytest command exits with an ordinary test failure. Collection or infrastructure errors are reported
separately and fail the mutation lane. Results are written to
`.test-artifacts/mutation-sample/summary.json` and `summary.md`.

This is a deliberately curated signal, not a global score. New launch-critical invariants should add
a specific realistic mutant and the narrowest authoritative test that kills it. Surviving mutants
block the lane until the oracle is strengthened or the mutant is documented as equivalent.

Equivalent or deliberately redundant guards are classified rather than converted into artificial
score-only tests. Current examples are the explicit Planning task, agent, and run comparisons that
are also covered by the exact target hash, the fresh-request guard that is independently enforced by
the service request journal, and the held-baseline guard derived from the same identity and placement
facts. These branches still need direct unit coverage when changed, but they are not counted as
public-contract mutation probes unless removing them changes an externally visible decision.

## Test support ownership

Reusable fixtures, stateful fakes, workflow builders, and scenario helpers live under
`tests/support/`. The test-package root is reserved for collected `test_*.py` modules,
pytest configuration, and the dedicated flake/mutation runners. A structural contract
rejects new root-level helper modules so support ownership cannot drift back into an
accidental second namespace.

### Native PostgreSQL concurrency helpers

Reusable deterministic synchronization lives in `tests/support/postgresql/concurrency.py`. Use
`TransactionGate` for explicit interleaving points, independent connections for separate server
transactions, and the named assertions for blocked, committed, aborted, stale-writer, lease-takeover,
and conditional-update outcomes. Compose these helpers with `core_db` or `native_workflow_db`, which
reset the disposable native schema through Alembic head and dispose the owning engine. Native
concurrency tests must not use sleeps as correctness evidence.

### Populated-predecessor migration framework

Reusable migration-lane support lives in `tests/support/postgresql/migrations.py`; migration-specific seed/assertion examples are in `projection_attempt_migration.py` and `honest_binding_migration.py`. See `tests/support/postgresql/MIGRATIONS.md` for the Agent A integration contract. SQLite remains compatibility evidence, PGlite remains development evidence, and only the native fixture is certification evidence.

### Semantic diagnostic and ORM-index contracts

`tests/test_semantic_invariant_diagnostic_coverage.py` requires every statically emitted durable semantic
invariant to have an explicit payload-safe diagnostic specification. Dynamic invariant families must be
documented and bounded. `tests/postgresql/test_orm_migration_index_alignment.py` protects migration-defined
indexes that must also exist in SQLAlchemy metadata.


### Service lifecycle seam checks

Run `tests/test_service_lifecycle_seams.py` with the request replay, lease atomicity, service lease, expiry, and
coordinator structure modules when changing request or lease lifecycle orchestration. The seam tests prove that
coordinators have typed dependencies and remain directly constructible; focused seam checks assert acquisition,
successful settlement, and cleanup call ordering, while behavioral modules continue to protect the underlying
transaction, replay, lease, error-conversion, and response semantics.

