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

`scripts/actions_cost_report.py` queries completed workflow runs and all job attempts for a requested range, derives runtime from each job's `started_at`/`completed_at`, and applies repository-owned per-job minute rounding. Rates and the included monthly allowance are explicit in `ci/actions-billing.json`.

Dollar fields are explicit: `gross_equivalent_cost_usd` is billed minutes multiplied by the configured runner rate before applying the allowance, while `approximate_overage_cost_usd` prices only billed minutes beyond the configured monthly allowance. Overage attribution is deterministic in job-start then job-ID order. A report must stay inside one UTC calendar month; if it starts after the beginning of that month, the caller must provide `monthly_billed_minutes_before_period` so the reporter does not invent prior allowance consumption. Cancelled started minutes remain visible and unknown runner labels fail closed.

`ci/fixtures/actions-run-31697885898-jobs.json` is a reduced historical GitHub API fixture. That successful seven-job CI run had 1,074 seconds of summed job runtime but 23 billed minutes after per-job rounding; the regression preserves that known incident-era minute example without falsely treating those 23 minutes as overage when allowance remains.
