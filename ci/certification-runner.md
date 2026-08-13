# Single-job certification runner primitives

These primitives sit below repository selection policy. They do not decide which lanes/groups a change requires, alter live workflow triggers, or replace exact-head Integration gate authority.

## Planner contract

`scripts/integration_certification.py` consumes the landed `repository-certification-plan-v1` contract produced by `scripts/integration_certification_plan.py`. It consumes that planner's `selected_groups` contract and does not implement path/lane selection policy.

The convergence/control-plane layer supplies two files:

1. the durable planner JSON; and
2. a `dish-certification-command-map-v1` object containing safe argv arrays for the groups selected by that exact plan.

Example command map:

```json
{
  "format": "dish-certification-command-map-v1",
  "commands": {
    "python-control-plane": [
      {
        "name": "focused evidence",
        "argv": ["dish/.venv/bin/python", "-m", "pytest", "tests/example.py", "-q"]
      }
    ]
  }
}
```

Unknown groups, commands for unselected groups, missing commands for selected groups, shell strings, malformed argv, and planner/group-order mismatches fail closed.

## Conditional runtime setup

`.github/actions/run-certification/action.yml` is a reusable composite action for the later one-hosted-job convergence workflow. It derives setup only from planner-selected groups:

- Python + canonical dependency bundle: Python/control-plane, native PostgreSQL, or browser acceptance;
- Node: frontend static or browser acceptance;
- isolated PostgreSQL 17.10: native PostgreSQL only;
- Chromium: browser acceptance only.

The action itself declares no `jobs:` or `runs-on:` and therefore does not create a separately billed hosted job.

## Execution and evidence

Execution follows the planner execution-group order and fails fast for Integration certification. After the first required failure, later selected groups are recorded `not_run_due_to_prior_failure`; unselected groups are recorded `not_selected`.

The runner computes `plan_digest` as SHA-256 of canonical JSON (`sort_keys=True`, compact separators, one trailing newline) and writes `dish-integration-certification-v1` evidence containing:

- exact candidate SHA from planner identity;
- Actions run ID and attempt;
- plan digest;
- required groups and deterministic execution order;
- every group result;
- per-group and total elapsed seconds;
- terminal passed/failed outcome.

`ci/integration-certification-evidence.schema.json` defines the combined evidence shape.

## Actions cost telemetry

`scripts/actions_cost_report.py` queries completed Actions runs/jobs for a requested period and applies repository-owned per-job rounded billing (`ceil` each started job to a whole minute). The explicit Linux rate and included monthly allowance live in `ci/actions-billing.json`.

Telemetry separates:

- `gross_equivalent_cost_usd`: billed minutes × configured rate before allowance;
- `overage_billed_minutes`: only minutes beyond the configured monthly allowance;
- `approximate_overage_cost_usd`: only excess-minute cost.

Allowance attribution is deterministic by job start time then job ID. Cross-month reports fail closed. A partial-month report must provide `--monthly-billed-before-period` so earlier allowance consumption is explicit. Unknown runner labels fail closed. Cancelled started jobs retain billed-minute attribution.
