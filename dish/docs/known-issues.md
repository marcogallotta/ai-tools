# Dish known issues

This file lists known gaps queued for future work, ordered by priority. An entry is not
implementation authorization. Current authority boundaries and runtime behavior remain defined by
[`architecture.md`](architecture.md) and [`runtime-contract.md`](runtime-contract.md).

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

### execution-recovery-audit-misattribution

**Priority: p2.**

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

### connected-request-status-inspection

**Priority: p3.**

A connected agent with a `request_id` has no read-only lookup for the request's authoritative
state. Exact replay remains the recovery contract, while investigation otherwise depends on private
tooling, logs, or inference from linked workflow records.

A future bounded lookup could report request status, command name, owner/run match, linked task and
operation identifiers, whether exact replay is safe, and any required private or human recovery.
It should not expose full canonical arguments or stored results by default. This is non-blocking
observability work; implement it only if post-launch response-loss investigations become frequent.
