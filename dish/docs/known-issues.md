# Dish known issues

This file separates open post-rollout candidates from limitations accepted for launch. An entry is
not implementation authorization. Current authority boundaries and runtime behavior remain defined
by [`architecture.md`](architecture.md) and [`runtime-contract.md`](runtime-contract.md).

## Post-rollout candidates

### DESIGN-003 — connected request-status inspection

A connected agent with a `request_id` has no read-only lookup for the request's authoritative
state. Exact replay remains the recovery contract, while investigation otherwise depends on private
tooling, logs, or inference from linked workflow records.

A future bounded lookup could report request status, command name, owner/run match, linked task and
operation identifiers, whether exact replay is safe, and any required private or human recovery.
It should not expose full canonical arguments or stored results by default. This is non-blocking
observability work; implement it only if post-launch response-loss investigations become frequent.

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

### REPRO-001 — connected recovery reproduction

The Action surface cannot safely inject pending or uncertain effects, inspect private journals, or
invoke administrative recovery, so a GPT-only live test cannot exercise repair/replay consistency
end to end. Local fault-injection tests and private admin tooling are the authoritative validation
surfaces.

This is a maintainer-confidence limitation, not a user-facing workflow defect. Do not widen the
public Action surface solely to reproduce these states. Investigate further only if a concrete
connected inconsistency is observed.
