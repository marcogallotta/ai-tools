# Dish future work

This file contains broader work that is **not already implemented** in the current Dish
architecture. Tracked gaps, post-rollout issue candidates, and accepted launch limitations belong in
[`known-issues.md`](known-issues.md). This is design triage, not implementation authorization. Any
item still requires Marco's explicit approval and should be justified by real usage evidence.

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

### Paginated section task listing

Add a private, read-only `dish list SECTION_GID` command so agents can fetch all incomplete tasks
waiting in Research Queue, Verification Queue, or any other Cooking section. It should validate the
section against the Cooking project, paginate to completion, fail rather than return a partial list,
and require an exact `dish read` before governed work begins.

See [`section-task-listing-design.md`](section-task-listing-design.md) for the proposed CLI,
completion filters, result contract, pagination behavior, private-surface boundary, and test scope.
This is intentionally smaller than natural-language `dish_find` and should be useful soon after
rollout without expanding workflow authority.

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

Do not infer nutrition from prose, add carbohydrate parsing, or build a general nutrition engine.
The field grammar belongs in the Honest task schema first; Dish should then parse and enforce that
exact shape.

### `WHAT TO BUY` / `QUANTITIES` reconciliation

Automation needs a real ingredient data model before it can compare these sections. A useful grammar
would distinguish:

- recipe use;
- current usable stock or yield;
- package/minimum purchase quantity;
- trim or waste;
- an explicit reason for any difference.

Literal numeric equality is not the invariant. Do not add a simplistic line-number or
number-matching rule.

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
request timeouts, and no private or admin routes. Add application-level request rate limiting only
if activation evidence shows abusive, accidental, or otherwise costly request volume. This is
defense in depth, not a prerequisite for the current single-owner rollout.

### Explicit unchanged-content re-Verification

If live use requires a fresh Verification of an unchanged, already signed task, add a guarded
`dish-admin reverify TASK_GID --reason ...` route. It should bind the exact current signed identity,
create a new operation and Verification cycle, set the task to `pending-verification`, move it to
Verification Queue, and then use the ordinary independent agent Verification flow.

Do not make a manual section move trigger re-Verification: placement alone carries no authenticated
intent or durable cycle evidence and may be accidental. Material post-signoff changes already enter
a new Verification cycle through the normal Change workflow; this proposal covers only unchanged
signed content.

### Natural-language dish lookup and generalized task-authority

Nothing here blocks rollout. The state-driven Custom GPT instructions (closing the "create" vs.
"existing task" decision gap) are worth doing soon after rollout, but not scheduled. The general
agent-surface `task_url`-to-`task_gid` extraction once bundled with that work is deferred as currently
unnecessary: agents already resolve Asana task URLs correctly without dedicated `dish`-side parsing,
revisit only if that stops holding up. The implemented `dish-admin expire-lease` parser does not
change this status: it is an operator-only, intentionally narrower two-form parser and rejects the
old optional `/f` suffix retained in the deferred agent design. The `create` collision check and the
`dish_find`/`TaskWorkflowSnapshot` authority generalization have no near-term timeframe at all —
worth having eventually, not a response to observed evidence, and meaningfully more work than the
instructions rewrite (the authority generalization touches the core action-authority invariant every
other command relies on).

See [`gpt-natural-interaction-design.md`](gpt-natural-interaction-design.md) for the complete
design, including the task-state/action precedence table, `dish_find`'s exact/fuzzy matching
contract, and why duplicate prevention stays deliberately best-effort rather than adding reservation
machinery. Implement any of this only if real recurring friction shows up, not on a schedule.

## In progress: abandoned-run recovery and long-term ownership

Part I of [`abandoned-run-ownership-design.md`](abandoned-run-ownership-design.md) — an explicit
`abandon-operation`/`reconcile-abandonment` path for a permanently lost chat run stranding
Planning, Research, or Verification — is a pre-rollout implementation candidate being built now,
not a deferred future item. Part II of that same document, a long-term attempt/session ownership
redesign that would let a replacement session (potentially a different agent) continue an
in-progress attempt instead of always forcing a fresh operation, is intentionally parked as a
post-rollout draft: re-open only after Part I ships and production evidence is available.

## Later architectural options

### Tool-mediated cooking and cook logs

Cooking agents currently read the signed task and write cook-log information outside the task body.
A future Dish surface could own cook-log entries and Marco-granted cooking overrides as first-class
operations.

Design questions include:

- the exact cook-log command and append-only record;
- how comments or a future backend represent actual quantities, deviations, results, and next
  action;
- how a Marco override names the exact waived gate without weakening task-body signoff;
- whether cooking reads need anything beyond the current exact task read.

This should not permit cooking agents to mutate the signed task body.

### Database-backed task store and separate frontend

Asana could eventually be replaced by a database-backed structured-dish store and a purpose-built
human frontend. Structured versioned data would become canonical, while Markdown or Asana notes
would be rendered views. The stable Dish command lifecycle and service boundary should remain the
agent interface so the backend change does not alter workflow semantics.

See [`database-backend-design.md`](database-backend-design.md) for the current draft authority,
storage, transaction, frontend, migration, and rollback design. It remains future design rather than
implementation or cutover authorization.

The draft permits an Asana-authoritative one-way shadow before cutover and an optional
DB-authoritative read-only Asana projection afterward. Neither stage permits peer writes or dual
authority.

Any replacement must preserve:

- exact structured dish identities plus imported source-document evidence;
- the guarded state machine and independent Verification;
- append-only evidence and recovery;
- audit history and safely classified external effects;
- a migration and cutover plan for the live corpus;
- Marco's reading, intervention, and cook-log needs.

Do not reproduce Asana's general project-management model unless real use requires it.

### Deployment and resilience beyond personal use

The current system is intentionally a single-owner personal service. Consider broader resilience
only if the deployment model changes or live evidence justifies it:

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
