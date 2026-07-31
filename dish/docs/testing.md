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

## Authoritative first-attempt gates

These commands never rerun a failure. Any failure blocks the gate.

```sh
.venv/bin/python -m pytest --smoke
.venv/bin/python -m pytest --database-boundary
.venv/bin/python -m pytest
```

A pass after retry is not a clean pass and must not be reported as one.

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

`test_concurrent_exact_first_calls_share_one_durable_challenge` (`DISH-flake-038-concurrent-challenge-backup-race`)
is currently a candidate: the flake-stress lane reproduced a `sqlite3.DatabaseError: legacy backup
schema version mismatch` race between concurrent `initialize_database` calls in
`_backup_legacy_database`. Under review.

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

No test is quarantined by this change.

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
.venv-flake/bin/python -m pytest --randomly-seed=<recorded-seed>
```

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
| Every code handoff | smoke, database-boundary, and complete first-attempt gates |
| Test infrastructure or concurrency change | rerun detector, five random-order runs, and twenty `flake_stress` runs |
| One unexpected failure | exact-node repeat workflow and recorded triage evidence |
| Nightly or periodic health check | rerun detector, random-order, stress, parallel diagnostic, quarantine lane, and static scan |
| Before release | all authoritative gates plus reviewed unresolved flake candidates and quarantine expiry check |

## Artifact retention

`.test-artifacts/` is ignored by Git. Preserve the relevant run directory with the associated issue
or handoff whenever it contains a failure. Do not commit generated JUnit XML or local environment
metadata.
