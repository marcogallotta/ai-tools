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
  --json /tmp/dish-asana-migration-1217419962189616.json \
  --csv /tmp/dish-asana-migration-1217419962189616.csv
```

The script reads the complete project task corpus, current task bodies and
comments, canonical GitHub task ownership, and the local lifecycle resolver
when available. It reconstructs an ordered lifecycle authority stream: current
notes are the untimestamped baseline, while comments and GitHub events are
ordered by their durable timestamps and provenance. Later authoritative holds,
prohibitions, dependencies, corrections, ownership transfers, or completion
therefore supersede older review/dispatch language. GitHub/lifecycle evidence
owns detailed execution truth once development starts; an Implementation
handoff alone remains `Ready` until worker-start evidence exists. A stale
`IMPLEMENTATION IN PROGRESS` claim without current worker/owning-PR proof is
reconciliation, not development. A GitHub-lineage failure or any task-comment
retrieval failure makes the generated plan invalid and exits nonzero; do not
use a partial ledger for migration decisions.

Review `ambiguous_tasks`, `validation_errors`, and every freshness-bound
semantic override before approving a migration. Overrides currently apply only
to the Development Workflow project and automatically stop applying when the
task body or latest comment changes. Other projects remain deterministic where
evidence is conclusive and surface unresolved meaning for Coordinator review.

`RECONCILIATION_REQUIRED` is a classification outcome, not a lifecycle
section. Its ledger entries have `applicable: false`; an apply consumer must
reject them until their evidence is reconciled and the planner produces a
normal section. Low confidence never produces an applicable lifecycle
destination. `Needs Processing` is reserved for genuinely raw intake.
Legacy section placement and a self-declared `READY`/`IMPLEMENTATION READY`
record do not independently prove readiness because the source workflow did
not provide a structured acceptance path. Ready requires current affirmative
dispatchability: accepted bounded scope, resolved required research/review,
current Implementation authority or dispatch evidence, no later hold,
prohibition, dependency, reopen, transfer, or terminal state. Source merge is
not Done when a current post-merge rollout or acceptance remainder exists.

The planner emits Priority, Code Area, and Version only from explicit durable
records or the corresponding Asana custom fields. In particular, a Version
record must identify the task's own version; contextual references such as
`Integration V1` or `Review V2` are not inferred as field values. The pure
apply/readback helpers cover section plus all three custom fields, but do not
perform live mutations.

The generated ledger is decision support only. Do not use it as authority to
move tasks or begin the migration without separate Coordinator cutover
approval.
