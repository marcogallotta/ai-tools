# Dish future work

This file contains broader **non-workflow** work that is not already implemented in the current Dish
architecture. Tracked gaps, post-rollout issue candidates, and accepted launch limitations belong in
[`known-issues.md`](known-issues.md). This is design triage, not implementation authorization; any item
still requires Marco's explicit approval and should be justified by real usage evidence.

For implemented behavior, read the [architecture index](architecture/index.md),
[`../README.md`](../README.md), and [`runtime-contract.md`](runtime-contract.md).

## Workflow and administration live in `workflow.md`

[`workflow.md`](workflow.md) is the single product source of truth for current **and deferred** Dish
workflow/admin behavior. Do not add a parallel workflow design here.

That includes Planning/Research/Verification semantics, Human Review/Evidence, proposal approval and
application, agent replacement/recovery, safe reclaim as operator UX, post-cutover journaling,
`dish-admin` behavior, project population/lifecycle, Cooked/archive semantics, phase-authoritative
listings, cook logs, re-Verification ideas, active Verification -> Planning, and speculative idea/cross-dish planning. Where implementation sequencing depends on PostgreSQL cutover, `workflow.md` records
that dependency directly.

Future agents should update `workflow.md` when one of those product decisions changes and keep this
file to architecture/data/capability ideas that do not define workflow semantics.

## Already implemented — do not re-propose as future work

The current release already includes:

- exact candidate/content identity through prepare, Verification, signoff, and submit;
- live content and Cooking-project placement drift detection;
- deterministic material-category enforcement for Small and post-signoff changes;
- explicit task-schema migration and historical database migration/reconciliation;
- durable write/movement attempts with `confirmed`, `not_applied`, and `uncertain` outcomes;
- recovery, audit repair, task operation locks, client/run leases, the current cross-run safe-reclaim
  mechanism, backup, and restore;
- the laptop-hosted shared service and bounded GPT Action surface;
- private CLI/admin and public Action listener separation;
- real generated Asana SDK contract tests and live test-project smoke procedure.

Future proposals should build on those mechanisms rather than reintroduce parallel paths. Product UX
for several of these mechanisms intentionally differs from the low-level implementation; see
[`workflow.md`](workflow.md).

## Sequencing relative to PostgreSQL authority cutover

Most features that depend directly on Asana section GIDs, Asana content identity, or the split
SQLite+Asana read model should not be built as temporary stopgaps if PostgreSQL cutover is near.
[`database-backend.md`](database-backend.md) replaces those authorities with Dish-owned task identity,
logical placement, task versions, and transactionally consistent workflow state.

New Dish-owned facts with no existing Asana-side representation — lightweight metadata, structured
sourcing/reference data, pending-order tracking — can be built independently if useful. Serving the
Honest repository read-only is also orthogonal to task authority.

Workflow-specific sequencing, including the explicit decision to wait until after cutover for rich
agent journals/resumability and Dish-owned Cooked/archive lifecycle, lives only in
[`workflow.md`](workflow.md).

### Overall program sequencing: dark launch through cutover

The intended order across the PostgreSQL program remains:

1. complete dark-launch activation and collect real evidence;
2. complete near-term frontend/read-path activation work that remains non-authoritative;
3. code-cleanup consolidation (`docs/code-cleanup-consolidation.md`);
4. code-cleanup maintainability (`docs/code-cleanup-maintainability.md`) as ongoing scheduled
   maintenance rather than a hard blocking phase;
5. cutover Phase 0 revalidation against whatever architecture survives code cleanup
   ([`postgresql-cutover.md`](postgresql-cutover.md) §11);
6. PostgreSQL cutover, only when Marco explicitly chooses.

Code-cleanup work may simplify current cutover/evidence machinery when real invariants prove it safe,
but must not silently change authority or deployment policy. Phase 0 exists to revalidate what is
still load-bearing rather than preserving migration-era complexity merely because it exists.

## Near-term/non-workflow capability candidates

### Lightweight dish metadata and fast filtering

Add a small Dish-owned metadata layer for destination/category/region tags, protein type, tier, and an
explicit availability blocker. Keep it in Dish's own database rather than encoding it into Asana title
text.

The availability blocker (for example "needs cilantro" or "needs fig season") is a concrete fact that
someone has deliberately set and may later clear. It is not automatic inference of real-world stock or
harvest state.

Fast category/region/tier/ingredient filtering avoids fetching the whole live Asana corpus before an
agent can narrow candidates. This remains useful after PostgreSQL cutover because the metadata is
Dish-owned rather than an Asana compatibility layer.

### Structured Sourcing/Reference catalog

Import or maintain Sourcing/Reference documents (for example halal seafood/meat sourcing) as a small
structured catalog: item, category, source, price estimate, availability note. The goal is fast agent
lookup, not importing prose merely for completeness.

### Pending-order / expected-delivery tracking

Track concrete external-order blockers: item, source, expected date, and status such as
ordered/arrived/resolved. This supports facts like "blocked until the Sichuan-store order arrives"
without inventing fuzzy stock estimation.

### Paginated Asana section task listing

A private read-only section listing remains useful while Asana is a live human surface: validate the
section against Cooking, paginate to completion, fail rather than return a partial list, and require an
exact task read before governed work starts.

This is a discovery/display capability, not workflow authority. Phase-authoritative task listing and
project/dashboard semantics are owned by [`workflow.md`](workflow.md), especially after cutover.
See [`section-task-listing-design.md`](section-task-listing-design.md) for the existing design details.

### Serve the Honest repository to agents

Expose the current `DISH_HONEST_PATH` checkout read-only through the Dish Action surface so agents can
list directories and request a bounded set of files by exact relative path. This replaces packaging,
versioning, and repeatedly uploading a `.tgz` snapshot to the Custom GPT configuration.

Keep the capability stateless and read-only. Resolve the real filesystem path and reject targets
outside the configured checkout root, including symlink escapes. Return a content hash with each file
so an agent can identify the exact source version it read. Bound multi-file reads by the Action
response-size limit rather than forcing one tool round trip per file.

### Bounded direct-dependency surfacing

Surface only direct candidates that can be identified deterministically, such as exact task IDs,
explicit Asana links, exact task-name references, and clearly named planning documents.

Keep the result advisory. Do not recurse, infer semantic impact, or block readiness/submission merely
because a possible dependency exists. Add disposition machinery only if real use shows it prevents
missed follow-up without creating busywork.

### Three-value nutrition grammar and deterministic enforcement

Add one approved canonical syntax for calories, protein, and fat for the complete served portion,
including stated sides, then let Dish parse/enforce that exact shape. The **semantic meaning of the
numbers and thresholds** is owned by [`workflow.md`](workflow.md) and the Honest protocol; this item is
only the structured field/grammar implementation.

Do not infer nutrition from arbitrary prose, add carbohydrate parsing, or build a general nutrition
engine. The grammar belongs in the Honest task schema first.

### `WHAT TO BUY` / `QUANTITIES` reconciliation

Automation needs a real ingredient data model before it can compare these sections. A useful grammar
would distinguish recipe use, current usable stock/yield, package/minimum purchase quantity, trim or
waste, and an explicit reason for differences.

Literal numeric equality is not the invariant. Do not add a simplistic line-number/number-matching
rule.

### Activation-derived observability implementation

The operator requirement for per-task verbose inspection and system-wide historical log synthesis is
owned by [`workflow.md`](workflow.md). Future implementation may add summaries for repeated recovery,
abandonment, audit-repair, latency/rate-limit, Action/schema, or materiality patterns **only when they
lead to a concrete operational decision**.

Use existing durable audit/attempt/history data as the source rather than creating a second event model.

### Public Action rate limiting

The Funnel-exposed Action listener already has a dedicated credential, route allowlist, body limits,
request timeouts, and no private/admin routes. Add application-level rate limiting only if activation
evidence shows abusive, accidental, or otherwise costly volume. This is defense in depth, not a
single-owner rollout prerequisite.

### Natural-language dish lookup and generalized task authority

Nothing here blocks rollout. State-driven connected-agent instructions that close the "create vs
existing task" interaction gap may be useful after rollout, but do not broaden task-authority machinery
without observed need.

The `create` collision check and a generalized `dish_find`/`TaskWorkflowSnapshot` authority remain
unscheduled. See [`gpt-natural-interaction-design.md`](gpt-natural-interaction-design.md) for the prior
design, matching contract, and duplicate-prevention rationale. Treat it as reference material, not an
automatic backlog commitment.

## Later architectural options

### Database-backed task store and separate frontend

PostgreSQL is the target authoritative database for Dish task documents and workflow state; Stage A
preserves the current title/body document representation before any separately authorized structured-
dish redesign. See [`database-backend.md`](database-backend.md),
[`database-backend-imp.md`](database-backend-imp.md), and
[`database-backend-migration.md`](database-backend-migration.md).

The separate frontend has its own draft staging design in [`frontend.md`](frontend.md) and is not a
Stage A authority prerequisite.

After cutover, Asana may remain a downstream read-only/human-facing projection during transition, but
it is never a peer write authority. Product lifecycle semantics such as Cooked/archive are defined in
[`workflow.md`](workflow.md), not by Asana project/section movement.

Any replacement must preserve:

- exact task identities plus imported source-document evidence;
- the guarded state machine and independent Verification;
- append-only evidence and recovery;
- audit history and safely classified external effects;
- a migration/cutover plan for the live corpus;
- Marco's reading/intervention needs;
- efficient category/destination browsing independent of transient Research/Verification queue
  placement.

Do not reproduce Asana's general project-management model unless real use requires it.

### Deployment and resilience beyond personal use

The current system is intentionally a single-owner personal service. Consider broader resilience only
if the deployment model changes or live evidence justifies it:

- sustained soak/load testing;
- automated handling of extended external-service outages;
- multi-host failover or replicated storage;
- disaster recovery beyond the chosen PostgreSQL backup/PITR plan;
- stronger public-network authentication;
- multi-user or hostile-client authorization.

These are not prerequisites for Marco's current deployment.

## Dropped, not deferred

Do not revive these without a new requirement:

- a writable legacy workflow or fallback mutation engine;
- a cached authoritative `managed_tasks` table;
- opposite-model-family Verification routing;
- cryptographic agent identity for the current trusted personal deployment;
- recursive dependency audits;
- a generic remote Asana proxy;
- a generic admin `unblock` command;
- broad semantic recipe judgment inside the deterministic tool;
- a dedicated cross-task `merge` operation for duplicate composite dishes — resolve through ordinary
  edit plus the lifecycle/archive behavior defined in [`workflow.md`](workflow.md);
- the superseded trusted-connected-session/operation-authority-assignment recovery redesign in
  [`abandoned-run-ownership-design.md`](abandoned-run-ownership-design.md) Part II unless Marco
  explicitly reopens that exact design.
