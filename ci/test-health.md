# Continuous test health telemetry

Continuous test health is an **advisory observability layer** over the existing test graph,
exact-head certification, full regression, and flake-detection contracts. It does not select tests,
remove required evidence, authorize workflow transitions, or gate Review/merge.

## Evidence flow

`scripts/integration_certification.py` records target and command wall time in the canonical hosted
exact-head job. Direct pytest commands also emit `--durations=0 --durations-min=0`; the executor
summarizes setup/call/teardown durations without changing the selected node/file set. The execution
spec carries the planner's existing stable target ID, profile, graph identity, selection reasons,
resource size, and declared parent/child target relationship into the evidence rather than creating a
second test identity.

`scripts/test_health_report.py pr-event` converts the exact plan plus execution evidence into one
`dish-test-health-run-v1` event. `full-event` does the same for the scheduled full-regression lanes,
binding each broad lane to the already-existing stable fallback/harness target that represents that
work. Full-regression pytest commands expose the same phase-duration rendering where they directly
invoke pytest.

The governed rerun detector remains the only source of a confirmed flake observation. Its
`summary.json` now contains a `confirmed_flakes` list only when pytest-rerunfailures emits an actual
rerun and the same node subsequently passes. A first-attempt failure alone is never a flake. Flake
summaries are accepted into a health event only for the same Git SHA and environment class.

Canonical hosted and local diagnostic observations are deliberately separate. Local measurements
may be inspected, but `test_health_report.py report` excludes them from hosted baselines.

## Recent-window report

The scheduled full-regression workflow uploads a compact `test-health-run-*` artifact, discovers up
to the newest 40 unexpired compact health artifacts, and builds a configurable 30-day report. Raw
execution logs remain in their existing bounded evidence artifacts; compact health artifacts retain
only the identities and measurements needed for history.

For each `(stable target ID, profile, comparable hosted environment)` series the report records:

- selection count/frequency, failure rate, confirmed-flake rate, and cumulative compute;
- successful elapsed samples, prior-sample median, median absolute deviation (MAD), and current delta;
- p90 at 10+ samples and p95 at 20+ samples;
- setup/call/teardown medians where pytest phase evidence exists;
- parent `child_targets`, child fan-out, and aggregate observed child cost;
- selection-reason counts/cumulative cost plus boundary-fallback, all-boundary-fallback,
  legacy-adapter, selector-gap, and explicitly triaged selector-miss rates;
- median selected-target count and target seconds per exact-head PR.

Selector-miss rows are accepted only from the existing validated full-regression triage contract and
preserve that record's full-regression run/main identity, responsible PR/head, exact certification
graph identity, missed stable target/guarantee, and correction owner/action. An unrelated baseline or
ordinary full-regression failure is never synthesized into a selector miss.

The report highlights the ten largest recent regression candidates, slowest medians, largest
cumulative-compute targets, frequently selected expensive targets, confirmed-flake rates, and
recurring failure rates. `INSUFFICIENT_DATA` is represented as `insufficient-data`; it is not treated
as healthy or regressed.

## Advisory regression thresholds

`ci/test-health-thresholds.json` is deliberately data/report configuration, not test policy. A target
becomes `regression-candidate` only when enough comparable successful samples exist and the latest
sample simultaneously exceeds:

1. the size-specific ratio floor;
2. the size-specific absolute-seconds floor; and
3. `baseline median + 3 × MAD`.

The initial floors are intentionally conservative (`small: 1.50 + 2s`, `medium: 1.35 + 10s`,
`large: 1.25 + 30s`) so low-noise evidence can accumulate before tuning. Changing these values can
change only advisory presentation. Any future quality promotion, merge gating, quarantine, or
selection change requires its own reviewed policy change.
