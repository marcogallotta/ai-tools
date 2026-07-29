# Dish future work

This file contains broader work that is **not already implemented** in the current Dish
architecture. Tracked gaps, post-rollout issue candidates, and accepted launch limitations belong
in [`known-issues.md`](known-issues.md). This is design triage, not implementation authorization.
Any item still requires Marco's explicit approval and should be justified by real usage evidence.

For the implemented system, read [`architecture.md`](architecture.md),
[`../README.md`](../README.md), and [`runtime-contract.md`](runtime-contract.md).

## Already implemented — do not re-propose as future work

The current release already includes:

- exact candidate/content identity through prepare, Verification, signoff, and submit;
- live content and Cooking-project placement drift detection;
- deterministic material-category enforcement for Small and post-signoff changes;
- explicit task-schema migration and historical database migration/reconciliation;
- durable write/movement attempts with `confirmed`, `not_applied`, and `uncertain` outcomes;
- recovery, audit repair, task operation locks, client/run leases, backup, and restore;
- the laptop-hosted shared service and bounded GPT Action surface;
- private CLI/admin and public Action listener separation;
- real generated Asana SDK contract tests and live test-project smoke procedure.

Future proposals should build on those mechanisms rather than reintroduce parallel paths.

## Near-term candidates after activation evidence

### Serve the Honest repository to agents

Expose the current `DISH_HONEST_PATH` checkout read-only through the Dish Action surface. Agents
should be able to list directories and request one file at a time with full repository read access.

This replaces packaging, versioning, and repeatedly uploading a `.tgz` copy to the Custom GPT
configuration. Repository changes become available immediately, with Dish preventing writes and
paths outside the configured checkout.

### Bounded direct-dependency surfacing

Surface only direct candidates that can be identified deterministically, such as exact task GIDs,
explicit Asana links, exact task-name references, and clearly named planning documents.

The result should remain advisory. It must not recurse, infer semantic impact, or block validation,
readiness, or submission merely because a possible dependency exists. Add a disposition format only
if live use shows that it reduces missed follow-up without creating busywork.

### Three-value nutrition grammar and enforcement

Add one approved canonical syntax for calories, protein, and fat per complete served portion,
including stated sides. Enforce the protocol's main-dish limits and matching approved exemptions.

Do not infer nutrition from prose, add carbohydrate parsing, or build a general nutrition engine. The
field grammar belongs in the Honest task schema first; Dish should then parse and enforce that exact
shape.

### `WHAT TO BUY` / `QUANTITIES` reconciliation

Automation needs a real ingredient data model before it can compare these sections. A useful grammar
would distinguish:

- recipe use;
- current usable stock or yield;
- package/minimum purchase quantity;
- trim or waste;
- an explicit reason for any difference.

Literal numeric equality is not the invariant. Do not add a simplistic line-number or number-matching
rule.

### Activation-derived observability

After real usage, decide whether operators need additional summaries for:

- repeated recovery-required outcomes;
- lease expiry patterns;
- audit-repair frequency;
- backend latency and Asana rate-limit events;
- materiality classifications that agents frequently dispute.

Add only summaries that lead to a concrete operational decision. The existing audit and attempt data
should remain the source rather than introducing a second event model.

### Public Action rate limiting

The Funnel-exposed Action listener already has a dedicated credential, route allowlist, body limits,
request timeouts, and no private or admin routes. Add application-level request rate limiting only if
activation evidence shows abusive, accidental, or otherwise costly request volume. This is defense
in depth, not a prerequisite for the current single-owner rollout.

## Later architectural options

### Tool-mediated cooking and cook logs

Cooking agents currently read the signed task and write cook-log information outside the task body.
A future Dish surface could own cook-log entries and Marco-granted cooking overrides as first-class
operations.

Design questions include:

- the exact cook-log command and append-only record;
- how comments or a future backend represent actual quantities, deviations, results, and next action;
- how a Marco override names the exact waived gate without weakening task-body signoff;
- whether cooking reads need anything beyond the current exact task read.

This should not permit cooking agents to mutate the signed task body.

### Database-backed task store and separate frontend

Asana could eventually be replaced by a database-backed document store and a purpose-built human
frontend. The stable Dish command/service contract should remain the agent interface so the backend
change does not alter workflow semantics.

See [`database-backend-design.md`](database-backend-design.md) for the current draft authority,
storage, transaction, frontend, migration, and rollback design. It remains future design rather than
implementation or cutover authorization.

Any replacement must preserve:

- the canonical task document and exact identities;
- the guarded state machine and independent Verification;
- append-only evidence and recovery;
- audit history and safely classified external effects;
- a migration and cutover plan for the live corpus;
- Marco's reading, intervention, and cook-log needs.

Do not reproduce Asana's general project-management model unless real use requires it.

### Deployment and resilience beyond personal use

The current system is intentionally a single-owner personal service. Consider broader resilience only
if the deployment model changes or live evidence justifies it:

- sustained soak and load testing;
- automated handling of Asana rate limits and extended outages;
- multi-host failover or replicated storage;
- disaster recovery beyond managed SQLite backups;
- a stronger public-network authentication model;
- multi-user or hostile-client authorization.

These are not prerequisites for Marco's current single-user deployment.

## Dropped, not deferred

The following should not be revived without a new requirement:

- a writable legacy workflow or fallback mutation engine;
- a cached authoritative `managed_tasks` table;
- opposite-model-family Verification routing;
- cryptographic agent identity for the current trusted personal deployment;
- recursive dependency audits;
- a generic remote Asana proxy;
- a generic admin `unblock` command;
- broad semantic recipe judgment inside the deterministic tool.
