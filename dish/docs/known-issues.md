# Dish known issues

This file separates post-rollout candidates, testing boundaries, and limitations accepted for
launch. An entry is not implementation authorization. Current authority boundaries and runtime
behavior remain defined by [`architecture.md`](architecture.md) and
[`runtime-contract.md`](runtime-contract.md).

## Rollout and triage context

Marco is the sole human operator and the sole person responsible for implementation, with AI agents
helping implement and use Dish concurrently. Concurrency safety and clear agent guidance are
therefore real requirements, but they do not imply a multi-operator product or a need to expose
every private administrative operation to agents.

The current workflow is already causing enough friction to block effective use. Prefer rollout over
pre-emptive completeness when a failure is unlikely, bounded, fail-closed, and recoverable by Marco
without losing, corrupting, duplicating, or wrongly assigning live production work. A small manual
step or delayed diagnosis is acceptable; recurring agent dead ends, substantial operator toil, or a
credible threat to production state are not.

Classify an issue for pre-rollout work only when its likely operational cost exceeds the cost of
delaying migration. Otherwise place it under post-rollout candidates or accept it for launch with a
clear workaround and revisit trigger. For every new or reconsidered issue, record:

- observed or expected recurrence, distinguishing a demonstrated pattern from a hypothetical edge;
- worst credible production effect, including any concurrency amplification;
- whether agents receive enough guidance to stop safely or ask Marco for a specific action;
- Marco's recovery effort and whether recovery requires private implementation knowledge;
- whether the proper fix belongs to the planned database backend or another later architecture;
- the concrete frequency, pain, or safety signal that should trigger reconsideration.

## Post-rollout candidates

### DESIGN-005 — explicit Planning-intent confirmation

**Priority: highest post-rollout candidate; not launch-blocking.**

An agent has started Planning for a task when Marco had not asked it to plan that task. The current
state-driven continuation contract can tell an agent that Planning is legal, but legality does not
establish user intent. This is a demonstrated failure with credible recurrence: it can open an
unwanted operation and lease, and a continuing agent can prepare and write an unwanted Planning
brief. Dish still governs the mutations, so this is not launch-blocking corruption, but it creates
avoidable cleanup and makes ordinary use feel unreliable.

Add a shared service-side, guaranteed two-call gate for `start` with `kind=planning`. The first call
must return `CONFIRMATION_REQUIRED` without opening an operation, acquiring a lease, or changing the
task. A fresh call may proceed only by referencing that durable challenge and supplying either
`intent_basis: user_requested`, or `intent_basis: agent_override` with a non-blank
`override_reason`. Enforce the same contract for the CLI and Custom Action. A lone optional
attestation field is insufficient because an agent could populate it on the first call and never
receive the intended challenge.

Implement this before lower-priority post-rollout candidates unless rollout evidence changes its
priority. Reconsider launch blocking only if another pre-rollout occurrence causes unwanted live
content or repeated operator cleanup.

### DESIGN-003 — connected request-status inspection

A connected agent with a `request_id` has no read-only lookup for the request's authoritative
state. Exact replay remains the recovery contract, while investigation otherwise depends on private
tooling, logs, or inference from linked workflow records.

A future bounded lookup could report request status, command name, owner/run match, linked task and
operation identifiers, whether exact replay is safe, and any required private or human recovery.
It should not expose full canonical arguments or stored results by default. This is non-blocking
observability work; implement it only if post-launch response-loss investigations become frequent.

## Testing boundaries

### REPRO-001 / TEST-001 — connected reproduction and local fault injection

The Action surface cannot safely inject pending or uncertain effects, inspect private journals, or
invoke administrative recovery, so a GPT-only live test cannot exercise repair/replay consistency
end to end. Local fake backends, targeted failure injection, restart fixtures, and private admin
tooling are the authoritative validation surfaces for pending requests, uncertain outcomes,
failed-first mutations, replay, recovery, and audit repair.

This coverage satisfies TEST-001 without adding a runtime fault-injection mechanism. The remaining
connected-reproduction gap is a maintainer-confidence limitation, not a user-facing workflow
defect. Do not expose a production Action that deliberately fails or corrupts mutations. Add a new
test mechanism only when a concrete recovery scenario cannot be exercised safely by the existing
local harness.

### TEST-002 — real-schema generated-SDK lifecycle coverage

The generated Asana SDK lifecycle test exercises `DishApplication` → `AsanaBackend` → generated SDK
→ stateful fake HTTP transport, while its release fixture uses `schema={}`. Real Honest schema
validation and the generated-SDK boundary are covered separately, not together in one lifecycle
test.

This is a low-risk test-composition gap, not evidence of a runtime defect or a rollout blocker.
Revisit if the SDK/schema boundary changes, a failure implicates their integration, or maintaining
the separate coverage becomes unreliable.

### VERIFY-001 — transient `service_database_unavailable` attribution

Controlled SQLite writer contention now reproduces `service_database_unavailable` safely before
execution or request consumption. After the writer releases, exact same-UUID retry succeeds with
one request record, one operation, and no duplicate mutation. Planning-reopen reconciliation is one
credible internal source because it deliberately holds a writer transaction across an Asana
network sequence, but it has not been proven to have caused the earlier spurious observation.

The retry-safety question is resolved for writer contention. Keep only the historical attribution
parked; reconsider implementation work if ordinary live use makes the condition frequent or a
future occurrence violates the confirmed fail-before-execution and exact-retry behavior.

### VERIFY-002 — transient non-material terminalization failure

SQLite writer contention now reproduces the test-project failure at `non_material_terminal` after
the candidate write and handoff validation commit. Dish preserves the confirmed write, prohibits
normal retry, does not duplicate content, and permits immediate recovery through the prescribed
private admin action even while the originating Action lease remains live. The remaining limitation
is diagnostic: durable evidence keeps only `OperationalError` rather than the available
`SQLITE_BUSY` or `SQLITE_LOCKED` category.

Keep exact SQLite-category retention as post-rollout diagnostic work. Reconsider on another live
occurrence or if the category is not a normalized writer-lock condition.

## Accepted for launch

### DESIGN-004 — private Planning reopen

Reopening a completed bare task for Planning has no connected Action. Dish identifies the required
private continuation and Marco runs `dish-admin reopen-planning`; the agent can then start Planning.

This is accepted as won't-fix for launch. Reopening a completed task is an explicit Marco lifecycle
decision, while a connected admin surface would add authentication and approval complexity for
little operational benefit. Revisit only if manual Planning reopens become frequent; a future human
frontend could expose the existing private operation without granting ordinary agents that
authority.

### DISH-003 — connected UUID schema visibility

The generated and served OpenAPI marks UUID fields with `format: uuid`, a canonical
lowercase/non-nil `pattern`, and exact length bounds. The GPT Action importer may expose only the
length bounds to its connected client, allowing malformed identifiers to reach Dish before being
rejected.

Backend UUID validation remains authoritative. The late feedback has low-to-moderate UX impact and
creates no workflow or replay state. Consider a future UUID representation redesign only if live
usage shows that connected-side validation would materially improve the experience.

### DISH-014 — private expired-lease recovery

Expired operation leases have no connected recovery Action. Dish fails closed and gives the agent
an empty action list, `required_admin_action: recover-lease`, the Marco/admin resolver, the exact
private admin command, and the actions available after recovery. Marco runs `dish-admin
recover-lease`; an eligible agent can then reclaim the operation and continue.

This is accepted as won't-fix for launch. The interruption requires a small manual step but does not
lose task content or duplicate work, and the agent can tell Marco exactly how to resolve it. Revisit
only if post-launch lease expiries create meaningful recurring operator friction.

### DISH-015 — private Evidence and Human Review resolution

Evidence and Human Review holds deliberately have no connected recovery Actions. The connected
agent stops and identifies the required Marco/admin continuation. Marco resolves the hold through
the narrow private `supply-evidence` or `record-human-decision` command, after which an eligible
agent continues the operation. Editing the Asana task directly is not authoritative because it
bypasses Dish's durable hold and audit state.

This is accepted as won't-fix for launch: the human checkpoint is intentional, the private recovery
is simple, and the expected operational impact is low. Revisit only if real post-launch holds create
meaningful recurring operator friction.

### DISH-018 — pending task creation recovery

If the service loses the authoritative result between Asana task creation, Research Queue
placement, and request completion, the connected caller cannot prove whether that pending create
applied. Dish fails closed rather than risk a duplicate. The failure mode is one bare or misplaced
task plus a blocked request requiring manual inspection.

This is accepted as low likelihood and low impact while Asana creation remains a multi-call
external effect. It disappears when task creation and request completion move to the transactional
database backend.
