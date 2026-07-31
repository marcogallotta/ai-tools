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

None currently queued.
