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

### Sequencing relative to a database-backend move

Most items below are built on the current Asana section/content-identity model that
[`database-backend-design.md`](database-backend-design.md)'s authority migration replaces (Dish
locations instead of section GIDs, `task_versions` instead of `content_versions`, restructured
operation/lease facts). If that migration becomes a near-term priority, treat these as parked until
its scope is decided rather than building against a model about to change:

- paginated section task listing;
- archive route for unapproved/redundant composite dishes;
- unchanged-content re-Verification admin route;
- activation-derived observability (its Asana-latency/rate-limit signals disappear post-cutover);
- bounded direct-dependency surfacing (partly keyed to Asana links as the reference identity);
- three-value nutrition grammar (lower urgency regardless, and possibly superseded by any later
  structured-representation migration).

A third category needs no such wait: new Dish-owned facts with no existing Asana-side representation
— lightweight dish tags and availability blockers, the Sourcing/Reference catalog, pending-order
tracking, and cook-log entries below. None of these migrate an existing external fact, so none of
them get thrown away by the authority migration; they can be built on whatever timeline makes sense
on their own.

Serving the Honest repository to agents is orthogonal to task storage and can proceed independently
at any time. Public Action rate limiting has no activation evidence justifying it regardless of
sequencing.

### Lightweight dish metadata and fast filtering

Independent of the backend-authority decision: add a small Dish-owned metadata layer (destination
category/region tags, protein type, tier, and an explicit availability blocker) directly in Dish's
own database, not Asana. None of this needs to wait on `database-backend-design.md`'s authority
migration, because it is new data with no existing Asana-side representation to migrate away from —
unlike section placement or content identity, there is nothing here to throw away later.

The availability blocker (e.g. "needs cilantro," "needs fig season") is a concrete, non-fuzzy fact:
someone already decided a dish can't be made right now and can clear that decision explicitly later.
It is not automatic proxying of real-world stock or harvest state, which stays out of scope pending
its own design pass. An earlier version of this idea shipped briefly as title-embedded `--blocker`
markers in `bin/dish-tool-imp.md` (see commit `481160a`, later removed with that file); a structured
field is a real improvement over baking it into rendered title text, since it needs no title
grammar, parsing, or re-rendering on every correction.

Fast category/region/tier/ingredient filtering solves the current problem where asking an agent to
search matches against the live Asana corpus is slow because it has to fetch everything first. A
lightweight tag layer gives agents a fast, structured, filterable answer instead.

### Structured Sourcing/Reference catalog

Import or maintain Sourcing/Reference documents (e.g. halal seafood and meat sourcing docs) as a
small structured catalog — item, category, source, price estimate, availability note — instead of
free prose agents have to read in full. This is a concrete answer to the open historical-corpus-scope
question in `database-backend-design.md` ("Needs human review" item 9): a real use for importing
these records is fast agent lookup, not just search/provenance completeness.

### Pending-order / expected-delivery tracking

Track a concrete, non-fuzzy blocker on an external order: item, source, expected date, status
(ordered/arrived/resolved). Lets an agent or Marco record "blocked until the Sichuan store order
arrives" and later re-check what's missing, without inventing a fuzzy stock-estimation feature.

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
should be able to list directories and request one or more files by exact relative path, with full
repository read access.

This replaces packaging, versioning, and repeatedly uploading a `.tgz` copy to the Custom GPT
configuration. Repository changes become available immediately, with Dish preventing writes and
paths outside the configured checkout.

Confirmed worth trying and not architecturally hard: this is a stateless read-only addition
alongside the existing `sections`/`read` Action routes, using the same command-registry and
generated-schema pattern, and it touches no workflow or mutation invariant. A GPT Action response is
capped at 100,000 characters per call with a 45-second timeout, so any call's total returned text
must stay well inside that cap; every current repo doc fits well inside it individually (the largest
is currently ~90KB). Today's `.tgz` upload only works because Code Interpreter unzips it in a sandbox
and the model navigates it through a multi-step generate/execute/observe loop, including a
per-session sandbox cold start — this is the concretely observed source of "surprisingly slow"
lookups, not just staleness. A direct list/read Action route collapses that into one deterministic
call and is expected to be both fresher and faster, not merely equally capable.

Support a bounded multi-file read (a handful of paths per call, response still bounded by the
100,000-character cap) alongside single-file read, not only "one file at a time." The typical agent
need is 3-10 known docs; one call per file would trade the round-trip cost of Code Interpreter's
multi-step navigation for a different multi-step cost — repeated model/tool cycles — instead of
actually collapsing it. Path validation must resolve the real filesystem path and reject anything
whose resolved target falls outside the checkout root; rejecting literal `..` in the requested path
is not sufficient, since a symlink inside the checkout can point outside it. Returning a content hash
alongside each file's path and text lets an agent state exactly which repository version it read,
without needing a database-backed snapshot mechanism.

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

### Verification `reverify` decision outcome (flagged concern, not a rejection)

This is a real observed need from live review, not a speculative addition: a verifier sometimes
wants to preserve the current candidate, record a specific concern (e.g. a fish naming/pan-method
question, a filling-balance judgment, herb-timing on service), and get a fresh independent look
without it counting as a rejection or triggering a correction. Today the only decision routes are
`approve` and `reject` (Small/Large/Evidence/Human Review); none of them mean "unchanged, re-queue,
don't count against it."

This is distinct from the unchanged-signed-content `reverify` above (that one is admin-triggered,
post-signoff, on content that already passed). This one fires mid-cycle, before signoff, as the
verifier's own decision. It should reuse the same successor-operation/cycle primitive that
[`abandoned-run-ownership-design.md`](abandoned-run-ownership-design.md) Part I shipped for
abandonment — cancel the source, create a fresh unbound Verification operation/cycle, preserve the
exact candidate — but triggered by verifier judgment instead of a lost run, and carrying the
concern forward as structured data on the successor (not a free-text comment the next agent has to
rediscover by re-reading the whole task). Part I's successor-cycle mechanism now exists; this
should not require Part II's cross-agent session redesign, since the triggering run is still live
and simply choosing not to sign off yet.

### Archive route for unapproved or redundant composite dishes

Also a real, observed need, not speculative: unapproved or redundant composite dishes currently
have no governed disposition — they just sit. Add a placement move into a dedicated Archived
section under the Cooking project, as a first-class governed Dish operation (exact Cooking-project
GID placement, like every other section move), not a raw Asana section/project metadata call —
`generic_asana_guard` already fails closed on exactly that path and must stay that way. Precondition
is likely "no open operation on the task," so an in-progress Planning/Research/Verification attempt
cannot be archived out from under an agent.

Duplicate composite dishes do not need a dedicated `merge` operation: resolve them with an ordinary
edit to the surviving task plus an archive of the redundant one. Adding real cross-task merge
authority would cut against the single-task operation model for no real gain.

### Return-to-Planning transition (open design question, not scheduled)

A Verification-time decision that a dish's *structure or purpose* needs redesign, not just a
correction — e.g. a macro-distorting quantity or a materially changed halal adaptation — currently
has no route back to Planning. Everything today that returns to Planning is `reopen-planning`:
Marco-only, and only for a completed task. Moving an *active* Verification attempt back to Planning
is a new class of transition; Part I's stage-policy successors are all same-stage (Planning→Planning,
Research→Research, Verification→new cycle), so Verification→Planning has no precedent to build on
yet.

Open questions before this is buildable: is the transition itself agent-legal, or does it need
Marco authorization the way other governed-Planning-fact changes do; and how do "replanning notes"
(the concern that triggered the send-back) get attached to the new Planning operation so the next
Planning pass doesn't have to re-derive the problem from scratch. No near-term timeframe — needs a
real design pass, not a quick add.

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

## Shipped; long-term ownership redesign superseded and abandoned

Part I of [`abandoned-run-ownership-design.md`](abandoned-run-ownership-design.md) — an explicit
`abandon-operation`/`reconcile-abandonment` path for a permanently lost chat run stranding
Planning, Research, or Verification — has shipped; it is no longer a deferred future item.

Part II of that same document, a long-term trusted-connected-session/operation-authority-assignment
redesign that would have let a replacement session (potentially a different agent) continue an
in-progress attempt instead of always forcing a fresh operation, is **superseded and abandoned by
human decision on 31 July 2026.** It is retained in that document only as historical context and
must not be resumed, extended, reviewed, or implemented unless Marco explicitly reopens that exact
design. The blocking reason was structural, not a fixable gap: the current GPT Action has no
authenticated per-chat identity to build session/authority replacement on, and closing that gap
would require a stateful broker Marco is not committing to build.

Current direction instead:

- Part I remains the supported recovery mechanism for as long as Asana is the authoritative task
  backend.
- [`database-backend-design.md`](database-backend-design.md) takes priority over any further
  recovery/ownership redesign.
- Future recovery design should be reconsidered only after that migration exists, and should build
  from a checkpoint model — intermediate Planning/Research/Verification-round work is journaled
  without changing canonical task content, canonical content advances only at a stage's or
  Verification round's committed boundary, and a later agent recovers from the last committed
  checkpoint and journaled evidence rather than requiring transfer of unfinished trusted authority
  — instead of the abandoned session/authority-assignment approach.

## Later architectural options

### Tool-mediated cooking and cook logs

Cooking agents currently read the signed task and write cook-log information outside the task body,
in a separate repository. There is also no governed way today to record that a dish was actually
cooked or to move it out of its Destination section: Marco does that manually by moving the task into
a separate Cooking History project himself, entirely outside Dish. That gap is accepted as fine for
the initial rollout, not launch-blocking — Dish's guarded lifecycle was always meant to stop at
submission for v1.

Post-rollout, expose a `log-cook` Action so the cooking agent logs how a cook went through Dish rather
than an external file, with the resulting placement move (to a Cooked section, or into Cooking
History) happening as a consequence of that log rather than a separate `mark-cooked` command.
Cook-log data is new Dish-owned data with no existing Asana-side representation, so building it does
not need to wait on the database-backend authority-migration decision (see the sequencing note
above).

However this is built, cooking or logging a cook must never require the task to be unblocked or past
any particular workflow state first. Keeping cooking agents outside governed workflow state today
means Marco can still cook a dish that is stuck in Verification or otherwise hitting rough edges; any
future `log-cook` action must preserve that same decoupling rather than gating on task state.

Design questions include:

- the exact cook-log command and append-only record, starting minimally (timestamp, agent, free-text
  outcome) and expanding only if needed;
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
- Marco's reading, intervention, and cook-log needs;
- category/destination browsing that is not blocked by a dish's current Research/Verification Queue
  placement.

Do not reproduce Asana's general project-management model unless real use requires it.

### Idea-dish intake and cross-dish planning (speculative)

Marco sometimes wants to dump a loosely-defined idea dish that isn't yet a real planned candidate,
and separately wants a higher-level planning agent that reasons across several dishes at once (e.g.
"define a themed block around rice pudding") rather than Dish's current one-task-at-a-time model.
Both are genuinely future and unformed: no schema, workflow, or authority shape has been decided.
Revisit only alongside the structured-representation and fast-filtering work above, once it's clear
what data a cross-dish planning agent would actually query.

Rough idea, needs fleshing out: a middle tier between a bare idea-dump and an actively progressing
task. Some planned dishes (e.g. a batch of Korean dishes penciled in months before Marco expects to
cook them) already carry real Planning detail — more than a skeleton — but should not sit in Research
or Verification Queue in the meantime, where agents scanning those queues would burn attention/cache
on them every pass. This wants some kind of per-category "held" location (e.g. a Korean-ideas line)
excluded from ordinary queue scanning while still letting the dish carry more structure than a bare
title. It overlaps but isn't fully solved by the availability-blocker tag above, since the concern
here is being physically out of the queues agents iterate over, not just a filterable flag on a
queued item. No location model, exclusion mechanism, or promotion-back-to-active-queue path is
decided yet.

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
- broad semantic recipe judgment inside the deterministic tool;
- a dedicated cross-task `merge` operation for duplicate composite dishes — resolve via ordinary
  edit plus archive instead.
