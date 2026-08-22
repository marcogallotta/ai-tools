# Periodic full regression and selector-miss feedback

The periodic full regression is a selector-quality backstop. It is **not** an Integration gate and does not alter ordinary PR/Integration certification selection.

## Trigger and duplicate suppression

`.github/workflows/full-regression.yml` runs approximately nightly at minute 17, deliberately away from the top of the hour, and supports `workflow_dispatch` for Coordinator-triggered extra runs after unusually high merge volume or a high-risk batch. Manual dispatch always performs the full run.

Scheduled execution is bound to the workflow event's exact `main` SHA. Before expensive setup, the workflow reads prior completed runs. If the same `main` SHA already has a successful completed full-regression run, the scheduled run exits cheaply. A failing full result is not deduplicated: unchanged failing `main` is eligible to run again, while Coordinator can always request an additional manual run.

## Execution and evidence

A substantive run forces all four diagnostic execution groups:

- `python-control-plane`
- `frontend-static-tooling`
- `native-postgresql`
- `browser-acceptance`

The full-regression workflow deliberately differs from Integration certification: a lane or setup failure is recorded but does not stop later groups from being attempted. Terminal failure is enforced only after combined evidence is written and uploaded.

`scripts/full_regression.py run-lane -- ...` is an adapter seam, not lane-policy authority. The command after `--` is the replaceable runner integration point for the shared certification runner. Full regression owns only force-all behavior, continue-after-failure behavior, timing/evidence aggregation, and triage contracts.

The uploaded `full-regression-<main-sha>` artifact contains `evidence.json` with schema `dish-full-regression-v1`. It records exact `main` SHA, prior completed full-regression SHA/run and Git range, run identity, all lane results, setup phase results, failures, elapsed timings, and approximate one-job billed minutes. The repository schema is `ci/schemas/full-regression-evidence-v1.schema.json`.

Lane status/timing remains aggregate execution evidence, but failure identity is **below the lane**. Structured runner outputs are harvested into one durable failure record per distinct failing/error invariant. Pytest and Node test suites use JUnit collection; native PostgreSQL certification uses its structured report; named non-structured boundaries record an explicit source/invariant failure. If a failed lane produces no finer record, finalization emits one `lane-command` fallback failure so a failure can never disappear from triage. Each failure record carries a stable `failure_id`, lane/component, source, invariant, failure kind, and optional detail. When evidence is strong enough, it also carries a typed `dish-ci-causal-fingerprint-v1` identity and its verified fingerprint. That identity normalizes owner surface, failing surface, invariant, and stable failure signature; run IDs and commit SHAs remain occurrence evidence and never fragment the cause. Stable structured detail distinguishes materially different failures of the same test. Coarse command/setup fallbacks have no causal fingerprint and must remain ambiguous. Multiple failures in one lane therefore remain independently classifiable without collapsing weak evidence into a repair owner.

The detailed-failure collector is part of the runner adapter seam: Agent C's shared runner may replace concrete commands, but it must preserve or emit the same distinct-failure records before finalization. It must not collapse a set of known failing tests/invariants back to one lane-level classification.

## Failure classification contract

Every distinct `failure_id` in `evidence.json` requires exactly one durable triage record using schema `dish-full-regression-triage-v1` (`ci/schemas/full-regression-triage-v1.schema.json`). Coverage is over the complete distinct failure set, not over failed lanes. Two failures in the same lane may therefore have different classifications, responsible PRs, and selector-miss dispositions. For a related regression, `failing_lane` and `failing_invariant` must match the referenced evidence failure exactly. The only allowed classifications are:

- **related regression** — the failing invariant was introduced or exposed by a change in the relevant `main` range. Record the responsible PR/head and the responsible exact-head certification plan/run.
- **unrelated baseline** — the failure pre-existed the responsible range or is otherwise demonstrably unrelated to those changes. The analysis must state the baseline evidence used to establish that conclusion.
- **environment-infrastructure** — the product invariant was not meaningfully exercised because runner, dependency, service, network, or other infrastructure failed. The analysis must identify that boundary rather than attributing a product regression.
- **ambiguous** — the occurrence is preserved, but evidence is not strong enough to assign a normalized cause or another classification. It cannot create or reuse a corrective owner.

Validate one classification record with:

```sh
python scripts/full_regression.py validate-triage \
  --evidence evidence.json \
  --triage triage/failure.json
```

Require complete coverage of every failure in the run with:

```sh
python scripts/full_regression.py check-triage \
  --evidence evidence.json \
  --triage-dir triage/
```

A run with failures is not triage-complete until that command succeeds. Asana remains live orchestration authority for assigning/following the defect; GitHub source/PRs remain the durable correction surface.

After an `unrelated baseline` record is validated, the event-driven Integrator routes it through the same corrective-owner function used by PR current-main recovery:

```sh
ASANA_ACCESS_TOKEN=... python scripts/full_regression.py route-triage \
  --evidence evidence.json \
  --triage triage/failure.json \
  --project-gid <development-workflow-project-gid>
```

The router verifies the fingerprint against the typed identity, reuses one owner across scheduled/manual/PR occurrences and changing main SHAs, records every exact occurrence idempotently, and reopens a completed owner into the project's unique `Ready` section through the existing fenced task-transition/readback contract. Related, infrastructure, and ambiguous records do not enter baseline ownership. This command is an event consumer; it adds no scheduler, queue, database, or PR certification authority.

## SELECTOR MISS

For every **related regression**, inspect the responsible PR/head's exact certification plan/run. If the failing invariant's required lane was absent from that plan, set `selector_miss: true`.

A durable SELECTOR MISS record binds:

- full-regression run ID and `main` SHA;
- responsible PR number and exact head SHA;
- failing invariant and missed lane;
- responsible certification plan ID, certification run ID, and candidate SHA;
- required selector correction owner/action;
- one or more selector policy paths that must change;
- a representative changed-path class, expected lane, and selector regression test path.

The correction is incomplete if it only fixes the product defect. It must also update selector ownership/traits/dependency/escalation policy **and** add the representative selector regression. Given the selector-miss triage record and the correction PR's complete changed-path set, enforce that durable requirement with:

```sh
python scripts/full_regression.py verify-selector-correction \
  --triage selector-miss.json \
  --changed-path dish/test_selection/ownership.csv \
  --changed-path dish/tests/test_selection/test_planner.py
```

The command fails unless at least one required policy path and the declared representative selector regression test are both present in the correction change set. This prevents a one-off selector miss from being closed without improving future selection quality.
