# Single-job certification runner primitives

These primitives are intentionally below selector and Integration policy. They do not decide which certification groups a change requires, alter live workflow triggers, or replace exact-head gate authority.

## Execution spec

`scripts/integration_certification.py` consumes `dish-certification-execution-spec-v1` JSON. The upstream planner/control plane supplies the exact candidate SHA, SHA-256 digest of its durable plan, and commands for only the groups it selected:

```json
{
  "schema": "dish-certification-execution-spec-v1",
  "candidate_sha": "<40-hex commit>",
  "plan_digest": "<64-hex sha256>",
  "required_groups": {
    "python-control-plane": [
      {"name": "focused evidence", "argv": ["dish/.venv/bin/python", "-m", "pytest", "..."]}
    ]
  }
}
```

Allowed groups, in execution order, are:

1. `python-control-plane`
2. `frontend-static`
3. `native-postgresql`
4. `browser-acceptance`

Commands are argv arrays, not shell strings. Unknown groups or malformed selected groups fail closed. Selection and command composition remain upstream policy; the runner only executes the supplied boundary commands.

## Conditional runtime setup

`.github/actions/run-certification/action.yml` derives setup from the selected groups and keeps all heavy setup conditional:

- Python + canonical dependency bundle: Python/control-plane, native PostgreSQL, or browser acceptance;
- Node: frontend static or browser acceptance;
- isolated PostgreSQL 17.10: native PostgreSQL only;
- maintained Chromium: browser acceptance only.

The action is a composite action, not a hosted job. It is inert until a workflow explicitly invokes it, so this primitive does not add a separate preflight job or change current CI dispatch.

## Execution and evidence

Integration execution is deterministic and fail-fast. When a selected group fails, later selected groups are recorded `not_run_due_to_prior_failure`; unselected groups are always recorded `not_selected`.

The runner writes `dish-integration-certification-v1` evidence containing candidate SHA, run ID/attempt, plan digest, required groups, deterministic execution order, every group result, per-group elapsed seconds, total elapsed seconds, and terminal outcome. Successful selected groups are `passed`; the first failing selected group is `failed`.

## Actions cost reporting

`scripts/actions_cost_report.py` queries workflow runs whose jobs can be attributable to one complete UTC calendar month, then fetches all job attempts and attributes billing by each job's `started_at`. Collection begins 35 days before month start, matching GitHub Actions' maximum workflow-run lifetime, so a run created before the month cannot hide a job that starts inside the month. Collection stops at the month end; jobs from the boundary-expanded run set that start before or after the declared month are excluded. GitHub run searches are split when needed to stay below the API's 1,000-result search cap, and an unsplittable capped interval fails closed. Runtime still comes from each attributed job's `started_at`/`completed_at`, with repository-owned per-job minute rounding. Rates and the included monthly allowance are explicit in `ci/actions-billing.json`.

The reporter requires one complete UTC calendar month so the configured monthly allowance can be applied without pretending a partial range is a full billing window. Per-workflow and per-job dollar figures are labeled `gross_equivalent_cost_usd`: billed minutes multiplied by the configured runner rate before any allowance. At the report total only, included minutes are consumed in deterministic job-start order and `approximate_overage_cost_usd` prices only billed minutes beyond the configured allowance. The report also exposes included minutes consumed/remaining, overage billed minutes, and billed minutes consumed by cancelled jobs. Unknown runner labels and incomplete/malformed in-month billable jobs fail closed rather than receiving guessed accounting.

`ci/fixtures/actions-run-31697885898-jobs.json` is a reduced historical GitHub API fixture. That successful seven-job CI run had 1,074 seconds of summed job runtime but 23 billed minutes after per-job rounding; the regression test preserves that known incident-era example.
