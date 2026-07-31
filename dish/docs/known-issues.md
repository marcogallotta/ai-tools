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

### planning-intent-confirmation

**Priority: p1; not launch-blocking.**

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

### distributed-transaction-ownership

**Resolved in the current base.**

SQLite control-statement ownership is now centralized in `dish_tool.transactions`. The runtime uses
named contracts for isolated nested units, caller-joined atomic units, and helpers that require an
existing transaction. Service request journaling, backup identity, lease mutation, operation
execution, abandonment succession, external-effect reconciliation, governed authorization, audit
repair, schema migration, and health write probing no longer hand-roll transaction control.

A structural regression test rejects raw `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`, or `RELEASE`
statements outside the transaction primitive module. Behavior-focused concurrency, replay,
crash-recovery, lease, abandonment, authorization, backup, migration, and audit-repair tests remain
the authority for the actual atomic units; the structural test prevents ownership from becoming
distributed again.

### oversized-recovery-functions

**Resolved in the current base.**

The eight audited hotspots were decomposed without changing their public behavior or transaction
boundaries: `_validate_semantic_evidence`, `recover_operation`, `execute_agent`, `execute_admin`,
`apply_operation_abandonment_succession_in_transaction`, `classify_abandonment_frontier`,
`claim_operation_execution`, and `execution_recovery_state`. Each is now a coordinator of less than
100 lines, with named helpers separating classification, validation, persistence, dispatch, and
result construction.

The refactor deliberately retained validation/error ordering, fault-injection seams, monkeypatch
surfaces used by recovery tests, and caller-owned transaction units. Focused concurrency,
recovery, service/admin, abandonment, schema, and database suites plus the complete test suite
remain the behavior authority; no test asserts helper layout as a substitute for workflow
invariants.

### connected-request-status-inspection

**Priority: p3; not launch-blocking.**

A connected agent with a `request_id` has no read-only lookup for the request's authoritative
state. Exact replay remains the recovery contract, while investigation otherwise depends on private
tooling, logs, or inference from linked workflow records.

A future bounded lookup could report request status, command name, owner/run match, linked task and
operation identifiers, whether exact replay is safe, and any required private or human recovery.
It should not expose full canonical arguments or stored results by default. This is non-blocking
observability work; implement it only if post-launch response-loss investigations become frequent.

### execution-recovery-audit-misattribution

**Priority: p2; not launch-blocking.**

`execution_recovery_state()` reconstructs durable effects it attributes to one execution, but has no
positive provenance for audit rows: it takes the operation-wide max audit row ID at execution start
and treats every newer row as execution evidence, except a fixed event-name prefix denylist
(`write_attempt.`, `movement_attempt.`, `dish.`, `dish-admin.`). A concurrent `marco.authorization`
grant, or a real verifier `inspect` committing `verification.inspected`/`dish_inspect_facts`, gets
attributed to an unrelated, unconnected execution that failed before any effect.

This is demonstrated by deterministic probe, not hypothetical, for both interleavings. The worst
effect is a false `BACKEND_UNCERTAIN`: recovery is reported required, retry is blocked, and the
agent is directed to run `dish-admin recover --outcome applied`, which is safe but a no-op. This is
fail-closed operator toil, not corruption — no duplicate or wrong effect occurs, and Marco's
recovery is one documented command needing no private knowledge. Recurrence needs a narrow race
(concurrent authorization or inspection against a failing execution), but any newly introduced audit
event type can silently reintroduce it by evading the denylist.

Fix with real provenance (e.g. an `operation_execution_id` column on relevant audit rows, or a
durable list of evidence IDs the execution produced) rather than extending the exclusion list.
Reconsider priority on any live false-recovery incident, or once authorization/inspection commands
routinely overlap live executions. Add regression coverage using real `authorize-governed-change`
and real verifier `inspect` commands racing a no-effect execution failure, rather than a synthetic
audit row — the existing synthetic test only re-asserts the current heuristic.

## Testing boundaries

### connected-reproduction-fault-injection

The Action surface cannot safely inject pending or uncertain effects, inspect private journals, or
invoke administrative recovery, so a GPT-only live test cannot exercise repair/replay consistency
end to end. Local fake backends, targeted failure injection, restart fixtures, and private admin
tooling are the authoritative validation surfaces for pending requests, uncertain outcomes,
failed-first mutations, replay, recovery, and audit repair.

This coverage satisfies the gap without adding a runtime fault-injection mechanism. The remaining
connected-reproduction gap is a maintainer-confidence limitation, not a user-facing workflow
defect. Do not expose a production Action that deliberately fails or corrupts mutations. Add a new
test mechanism only when a concrete recovery scenario cannot be exercised safely by the existing
local harness.

### generated-sdk-real-schema-coverage

The generated Asana SDK lifecycle test exercises `DishApplication` → `AsanaBackend` → generated SDK
→ stateful fake HTTP transport, while its release fixture uses `schema={}`. Real Honest schema
validation and the generated-SDK boundary are covered separately, not together in one lifecycle
test.

This is a low-risk test-composition gap, not evidence of a runtime defect or a rollout blocker.
Revisit if the SDK/schema boundary changes, a failure implicates their integration, or maintaining
the separate coverage becomes unreliable.

### service-database-unavailable-attribution

Controlled SQLite writer contention now reproduces `service_database_unavailable` safely before
execution or request consumption. After the writer releases, exact same-UUID retry succeeds with
one request record, one operation, and no duplicate mutation. Planning-reopen reconciliation is one
credible internal source because it deliberately holds a writer transaction across an Asana
network sequence, but it has not been proven to have caused the earlier spurious observation.

The retry-safety question is resolved for writer contention. Keep only the historical attribution
parked; reconsider implementation work if ordinary live use makes the condition frequent or a
future occurrence violates the confirmed fail-before-execution and exact-retry behavior.

### non-material-terminalization-transient-failure

SQLite writer contention now reproduces the test-project failure at `non_material_terminal` after
the candidate write and handoff validation commit. Dish preserves the confirmed write, prohibits
normal retry, does not duplicate content, and permits immediate recovery through the prescribed
private admin action even while the originating Action lease remains live. The remaining limitation
is diagnostic: durable evidence keeps only `OperationalError` rather than the available
`SQLITE_BUSY` or `SQLITE_LOCKED` category.

Keep exact SQLite-category retention as post-rollout diagnostic work. Reconsider on another live
occurrence or if the category is not a normalized writer-lock condition.

### abandonment-suite-fabricated-states

Several abandonment tests construct database state directly (operation phase, Verification-cycle
outcome, cycle/step creation, abandonment records) rather than through governed producers, then make
workflow-level claims from that fabricated shape. Separately, some service-level abandonment tests
patch `_assert_mutation_ready`, `_release`, and `settle_abandonment_frontier` — reasonable for
narrow unit tests, but those tests cannot be read as full authority, compatibility, or
service-boundary validation.

This is a coverage-confidence gap, not a demonstrated runtime defect: a green run over hand-built
state or heavily mocked authority does not prove a real producer creates the same evidence graph, or
that the claimed authority check actually runs. Distinguish persistence-invariant tests (direct SQL
is fine) from producer-contract tests (must use the real command path); the strongest task-fence,
crash, and replay tests should use real release resolution and real service admission logic.

Revisit if a live incident occurs in an area whose only coverage is hand-built or mocked, or when
adding a new abandonment-adjacent authority check — give it at least one producer-contract test
using the real command path before treating it as proven.

## Accepted for launch

### private-planning-reopen

Reopening a completed bare task for Planning has no connected Action. Dish identifies the required
private continuation and Marco runs `dish-admin reopen-planning`; the agent can then start Planning.

This is accepted as won't-fix for launch. Reopening a completed task is an explicit Marco lifecycle
decision, while a connected admin surface would add authentication and approval complexity for
little operational benefit. Revisit only if manual Planning reopens become frequent; a future human
frontend could expose the existing private operation without granting ordinary agents that
authority.

### connected-uuid-schema-visibility

The generated and served OpenAPI marks UUID fields with `format: uuid`, a canonical
lowercase/non-nil `pattern`, and exact length bounds. The GPT Action importer may expose only the
length bounds to its connected client, allowing malformed identifiers to reach Dish before being
rejected.

Backend UUID validation remains authoritative. The late feedback has low-to-moderate UX impact and
creates no workflow or replay state. Consider a future UUID representation redesign only if live
usage shows that connected-side validation would materially improve the experience.

### expired-lease-vs-permanent-abandonment

Expired lease recovery and permanent run abandonment are separate authorities. `dish-admin
recover-lease` releases only lease liveness and is correct when the same durable run will return. It
never transfers workflow ownership. When the original chat/run is permanently unavailable, Marco
uses `dish-admin abandon-operation`; Dish verifies the latest expired or released actor attempt and
then either creates a clean exact-target successor, preserves/finalizes a committed route, preserves
a governed hold, or blocks for `dish-admin reconcile-abandonment`. The abandoned owner/run cannot
claim the successor or continuation.

Agent-facing responses never expose these private commands as connected `allowed_actions`. They
return the exact admin command and relay instruction. After Marco confirms success, the agent must
refresh the authoritative Dish action and follow the exact continuation returned. Partial, uncertain,
or contradictory external effects remain fenced rather than being guessed or compensated.

### private-evidence-human-review-resolution

Evidence and Human Review holds deliberately have no connected recovery Actions. The connected
agent stops and identifies the required Marco/admin continuation. Marco resolves the hold through
the narrow private `supply-evidence` or `record-human-decision` command, after which an eligible
agent continues the operation. Editing the Asana task directly is not authoritative because it
bypasses Dish's durable hold and audit state.

This is accepted as won't-fix for launch: the human checkpoint is intentional, the private recovery
is simple, and the expected operational impact is low. Revisit only if real post-launch holds create
meaningful recurring operator friction.

### pending-task-creation-recovery

If the service loses the authoritative result between Asana task creation, Research Queue
placement, and request completion, the connected caller cannot prove whether that pending create
applied. Dish fails closed rather than risk a duplicate. The failure mode is one bare or misplaced
task plus a blocked request requiring manual inspection.

This is accepted as low likelihood and low impact while Asana creation remains a multi-call
external effect. It disappears when task creation and request completion move to the transactional
database backend.
