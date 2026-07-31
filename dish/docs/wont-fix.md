# Dish won't-fix and accepted test-coverage gaps

This file archives items considered and deliberately not acted on: won't-fix decisions and
test-coverage gaps judged acceptable. It exists so a future review doesn't re-surface these as new
findings. It is not actively maintained, can go stale, and is not implementation authorization.
Current authority boundaries and runtime behavior remain defined by
[`architecture.md`](architecture.md) and [`runtime-contract.md`](runtime-contract.md).

Every write-up below was drafted by an AI agent, not Marco. He has not reviewed or approved any
individual severity claim or blurb; treat them as rough, unvetted starting points rather than his
considered judgment.

### connected-reproduction-fault-injection-test-coverage-gap

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

### connected-uuid-schema-visibility-wont-fix

The generated and served OpenAPI marks UUID fields with `format: uuid`, a canonical
lowercase/non-nil `pattern`, and exact length bounds. The GPT Action importer may expose only the
length bounds to its connected client, allowing malformed identifiers to reach Dish before being
rejected.

Backend UUID validation remains authoritative. The late feedback has low-to-moderate UX impact and
creates no workflow or replay state. Consider a future UUID representation redesign only if live
usage shows that connected-side validation would materially improve the experience.

### generated-sdk-real-schema-test-coverage-gap

The generated Asana SDK lifecycle test exercises `DishApplication` → `AsanaBackend` → generated SDK
→ stateful fake HTTP transport, while its release fixture uses `schema={}`. Real Honest schema
validation and the generated-SDK boundary are covered separately, not together in one lifecycle
test.

This is a low-risk test-composition gap, not evidence of a runtime defect or a rollout blocker.
Revisit if the SDK/schema boundary changes, a failure implicates their integration, or maintaining
the separate coverage becomes unreliable.

### service-database-unavailable-attribution-test-coverage-gap

Controlled SQLite writer contention now reproduces `service_database_unavailable` safely before
execution or request consumption. After the writer releases, exact same-UUID retry succeeds with
one request record, one operation, and no duplicate mutation. Planning-reopen reconciliation is one
credible internal source because it deliberately holds a writer transaction across an Asana
network sequence, but it has not been proven to have caused the earlier spurious observation.

The retry-safety question is resolved for writer contention. Keep only the historical attribution
parked; reconsider implementation work if ordinary live use makes the condition frequent or a
future occurrence violates the confirmed fail-before-execution and exact-retry behavior.

### private-planning-reopen-wont-fix

Reopening a completed bare task for Planning has no connected Action. Dish identifies the required
private continuation and Marco runs `dish-admin reopen-planning`; the agent can then start Planning.

This is accepted as won't-fix for launch. Reopening a completed task is an explicit Marco lifecycle
decision, while a connected admin surface would add authentication and approval complexity for
little operational benefit. Revisit only if manual Planning reopens become frequent; a future human
frontend could expose the existing private operation without granting ordinary agents that
authority.

### expired-lease-vs-permanent-abandonment-wont-fix

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

### private-evidence-human-review-resolution-wont-fix

Evidence and Human Review holds deliberately have no connected recovery Actions. The connected
agent stops and identifies the required Marco/admin continuation. Marco resolves the hold through
the narrow private `supply-evidence` or `record-human-decision` command, after which an eligible
agent continues the operation. Editing the Asana task directly is not authoritative because it
bypasses Dish's durable hold and audit state.

This is accepted as won't-fix for launch: the human checkpoint is intentional, the private recovery
is simple, and the expected operational impact is low. Revisit only if real post-launch holds create
meaningful recurring operator friction.

### connected-request-status-inspection-wont-fix

A connected agent with a `request_id` has no read-only lookup for the request's authoritative
state. Exact replay remains the recovery contract, while investigation otherwise depends on private
tooling, logs, or inference from linked workflow records.

A future bounded lookup could report request status, command name, owner/run match, linked task and
operation identifiers, whether exact replay is safe, and any required private or human recovery. It
should not expose full canonical arguments or stored results by default.

This is accepted as won't-fix for now: it is non-blocking observability work with an existing
workaround. Revisit only if post-launch response-loss investigations become frequent.

### pending-task-creation-recovery-wont-fix

If the service loses the authoritative result between Asana task creation, Research Queue
placement, and request completion, the connected caller cannot prove whether that pending create
applied. Dish fails closed rather than risk a duplicate. The failure mode is one bare or misplaced
task plus a blocked request requiring manual inspection.

This is accepted as low likelihood and low impact while Asana creation remains a multi-call
external effect. It disappears when task creation and request completion move to the transactional
database backend.
