# Dish accepted limitations

These are understood limitations of the current Dish deployment, not active implementation
commitments. Each entry records the practical effect, the safe handling, and the evidence that
would justify revisiting it. Current authority boundaries and runtime behavior remain defined by
[`architecture.md`](architecture.md) and [`runtime-contract.md`](runtime-contract.md).

## DISH-003 — connected UUID schema visibility

The generated and served OpenAPI marks UUID fields with `format: uuid`, a canonical
lowercase/non-nil `pattern`, and exact length bounds. The GPT Action importer may expose only the
length bounds to its connected client, allowing malformed identifiers to reach Dish before being
rejected.

Backend UUID validation remains authoritative. The late feedback has low-to-moderate UX impact and
creates no workflow or replay state. Consider a future UUID representation redesign only if live
usage shows that connected-side validation would materially improve the experience.

## DISH-015 — private Evidence and Human Review resolution

Evidence and Human Review holds deliberately have no connected recovery Actions. The connected
agent stops and identifies the required Marco/admin continuation. Marco resolves the hold through
the narrow private `supply-evidence` or `record-human-decision` command, after which an eligible
agent continues the operation. Editing the Asana task directly is not authoritative because it
bypasses Dish's durable hold and audit state.

This is accepted as won't-fix for launch: the human checkpoint is intentional, the private recovery
is simple, and the expected operational impact is low. Revisit only if real post-launch holds create
meaningful recurring operator friction.

## DISH-018 — pending task creation recovery

If the service loses the authoritative result between Asana task creation, Research Queue
placement, and request completion, the connected caller cannot prove whether that pending create
applied. Dish fails closed rather than risk a duplicate. The failure mode is one bare or misplaced
task plus a blocked request requiring manual inspection.

This is accepted as low likelihood and low impact while Asana creation remains a multi-call
external effect. It disappears when task creation and request completion move to the transactional
database backend.

## REPRO-001 — connected recovery reproduction

The Action surface cannot safely inject pending or uncertain effects, inspect private journals, or
invoke administrative recovery, so a GPT-only live test cannot exercise repair/replay consistency
end to end. Local fault-injection tests and private admin tooling are the authoritative validation
surfaces.

This is a maintainer-confidence limitation, not a user-facing workflow defect. Do not widen the
public Action surface solely to reproduce these states. Investigate further only if a concrete
connected inconsistency is observed.
