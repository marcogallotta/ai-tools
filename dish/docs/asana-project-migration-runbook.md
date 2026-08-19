# Asana project migration planning

`scripts/dish-asana-migration-plan` produces a read-only JSON/CSV ledger for
moving an existing Dish Asana project into the standard lifecycle sections. It
does not mutate Asana, GitHub, repository policy, or project settings.

Run it from `dish/` with the exact project GID. Keep generated ledgers outside
the repository because they contain live task content and become stale:

```sh
scripts/dish-asana-migration-plan \
  --project-gid 1217419962189616 \
  --project-name "Dish — Development Workflow" \
  --target-version v2 \
  --json /tmp/dish-asana-migration-1217419962189616.json \
  --csv /tmp/dish-asana-migration-1217419962189616.csv
```

The script reads the complete project task corpus, current task bodies and
comments, the live project name/modified time/section structure, canonical
GitHub task ownership, and the local lifecycle resolver
when available. GitHub/lifecycle evidence owns detailed execution truth once
development starts; an Implementation handoff alone remains `Ready` until
worker-start evidence exists. A GitHub-lineage failure or any task-comment
retrieval failure makes the generated plan invalid and exits nonzero; do not
use a partial ledger for migration decisions.

Review `ambiguous_tasks`, `validation_errors`, and every freshness-bound
semantic override before approving a migration. Overrides currently apply only
to the Development Workflow project and automatically stop applying when the
task body or latest comment changes. Other projects remain deterministic where
evidence is conclusive and surface unresolved meaning for Coordinator review.

The generated ledger is decision support only. Do not use it as authority to
move tasks or begin the migration without separate Coordinator cutover
approval.

The JSON binds the source project GID and live snapshot to the exact target
name. `v2` targets `Dish — Development Workflow v2`; an exact completed v2
structure is reported as already complete, while a v2 name with mixed sections
fails validation. `v3` may be planned, but its `apply_supported` field is false;
every generated plan records `apply_authorized` as false.
The cutover contract records the original name for rollback and requires the
project rename to be the final completion signal. This repository does not
provide an apply command; applying either plan remains a separately authorized
operation.
