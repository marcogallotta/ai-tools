# Dish known issues

This file lists known gaps queued for future work, ordered by priority. An entry is not
implementation authorization. Current authority boundaries and runtime behavior remain defined by
[architecture index](architecture/index.md) and [`runtime-contract.md`](runtime-contract.md).

Won't-fix decisions and accepted test-coverage gaps are archived in
[`wont-fix.md`](wont-fix.md), not listed here — check there before re-proposing one of those as a
new finding.

Every entry's priority label and write-up below was drafted by an AI agent, not Marco. He has not
reviewed or approved any individual priority, severity claim, or blurb; treat them as rough,
unvetted starting points rather than his considered judgment.

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

## Known issues, ordered by priority

### Shadow worker never classifies or gaps out permanent delivery errors

- Observed: production dark-launch worker crash-looped for days after a pending PostgreSQL
  migration (`0041_test_generation_rollover`) was merged but never applied to production
  (2026-08-18/19 incident). Separately, once the migration was applied, 11 spool records
  (rollout_sequence 173-183) captured under a prior, now-invalid
  `source_authority_generation` were retried by the worker indefinitely (one record hit 57,963
  failed attempts) and blocked every record behind them in FIFO order, because
  `ShadowSpool.pending()` (`dish_service/shadow_spool.py`) always returns the lowest
  `rollout_sequence` item first and `shadow_worker.run_once`/`_deliver`
  (`dish_pg/shadow_worker.py`) has no exception classification — every exception, including a
  non-retryable `TransitionAuthorityError` for a generation mismatch, is caught generically and
  recorded via `mark_delivery_failed`, which only increments `delivery_attempts`; the item is
  never converted to an explicit gap or skipped. Both instances were resolved manually this
  session (migration applied; the 11 stale records closed as explicit `uncomparable` gaps via
  `ShadowService.record_gap` + `ShadowSpool.mark_delivered`).
- Worst effect: on any future baseline resync (or comparable permanent, non-retryable delivery
  error), the same head-of-line block recurs and silently stalls all dark-launch comparison data
  behind it until someone notices the backlog isn't draining and manually intervenes.
- Agent guidance: none — nothing distinguishes a transient delivery failure from a permanent one
  in the worker's error handling.
- Recovery: currently requires a human/agent to notice the stalled backlog, diagnose the specific
  permanent-error class, and manually call `record_gap`/`mark_delivered` to unblock the queue —
  as done manually this session.
- Revisit trigger: recurs on the next baseline resync unless `_deliver` classifies
  `TransitionAuthorityError` (and any other permanent error class) and auto-converts it to a gap
  instead of retrying forever.

### `workflow_operations` unique-open-operation constraint scoped too broadly

- Observed: shadow delivery for task `d000737e-c2f4-409a-92a5-b0f182b14ea1` failed with
  `psycopg.errors.UniqueViolation` on `uq_workflow_operations_one_open_per_task`. PG already had
  an open `planning`-kind operation for the task; production went on to legitimately open a
  separate `initial`-kind operation on the same task concurrently, which the constraint (scoped
  to `(generation_id, task_id)` only, not including `kind`) rejects as a duplicate.
- Worst effect: any task with concurrent operations of different kinds fails shadow delivery with
  a hard DB error rather than a clean comparison outcome; this is a shadow/PG schema-modeling gap
  against real production behavior, not spool/replay noise.
- Agent guidance: none.
- Recovery: N/A on the shadow side (read-only comparison); no production impact observed.
- Revisit trigger: re-scope the constraint to `(generation_id, task_id, kind)`, or confirm and
  document that only one *kind* is meant to be open at a time and treat production's concurrent
  case as the actual bug — needs an owner decision, not just a schema tweak.

### Content-identity hash diverges between source and target for identical content

- Observed: sampled dark-launch parity mismatches (2026-08-18/19) where source and target
  `task_content` entries have byte-identical `title` and `body` but different `identity` hash
  values (e.g. `470f819f...` vs `848312e6...` for the same "Warm potato salad with yarrow"
  content). Seen consistently across every sampled case with matching title/body.
- Worst effect: dark-launch parity comparison reports these as mismatches even though the
  underlying content is identical — inflates the apparent mismatch rate and masks genuine content
  divergence among the noise. Suggests the two paths compute the identity hash over different
  inputs (extra field, key ordering, or algorithm difference).
- Agent guidance: none.
- Recovery: N/A — comparison-only, no production impact.
- Revisit trigger: needs whoever owns the identity-hash computation on each path to compare
  inputs directly and align them; until fixed, this will keep polluting mismatch counts on every
  future comparison run.

### Large-correction route used for pre-Dish migration backfill, not material recipe change

- Observed: real instance — [Phở gà repeat — pressure-cooker correction](https://app.asana.com/1/1200569426771227/project/1217084805070730/task/1217084919442831).
  Its own `Material changes` entry states the Large route was triggered by filling in a legacy
  `WHAT TO BUY` placeholder and aligning the title to satisfy `title.recognition` — pure
  migration backfill, not a change to what gets cooked. The Large route is specified
  (`dish-verification-protocol.md:203-207`) for changes that materially affect identity,
  quantities, safety, sourcing, etc. — not schema completeness left over from the pre-Dish
  migration.
- Worst effect: friction only. Observed cost was low in this instance — a second verifier
  re-verified and approved without incident. Marco: not hard to deal with for now; conceptually
  mismatched classification, not a functional failure or a current blocker.
- Agent guidance: none exists distinguishing "real Large content change" from "backfilling what
  the migration never populated."
- Recovery: N/A — not a failure mode.
- Revisit trigger: track recurrence across future corrections. If backfill-only Large
  corrections keep showing up, consider a lighter route or a distinct `migration-backfill`
  classification.
