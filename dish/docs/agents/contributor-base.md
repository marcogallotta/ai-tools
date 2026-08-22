# Dish contributor base contract

This is the inherited contract for any standing Dish role that can modify repository state.

Specialist contracts add scope-specific rules; they do not replace these contributor rules.

## Repository freshness

Establish the authoritative base at the start of work. Do not continuously poll `origin` or react to unrelated commits while implementing.

Fetch/synchronize when:

- starting a task;
- resuming after a substantial interruption;
- explicitly asked to sync/rebase/merge;
- preparing integration handoff.

A moving remote branch is an integration concern unless it directly affects the current task.

## Assigned-task dismissal gate

Before concluding an assigned task is invalid, no-op, already fixed, not reproducible, or otherwise has nothing to do, read the task's current notes/problem statement and the material history/evidence relevant to why it exists. Reconcile that record with current GitHub/runtime observations. Current live state remains authoritative for current facts, but a healthy present state does not by itself erase a documented historical, replay, shadow, or process defect. If the sources appear inconsistent, investigate and explain the discrepancy before dismissing the task.

This is a high-risk decision gate, not a requirement to reread full task history before every routine action.

## Authorized fallback gate

Before saying `cannot`, `blocked`, `tool unavailable`, or that Marco must perform a routine authorized operation, inspect the currently available relevant actions/tools. Separate the required semantic outcome from one preferred transport and use an equivalent authorized fallback when it preserves the same safety, authority, durability, and workflow invariant. After a state-changing fallback, verify the write response or authoritative readback before claiming completion. A chat-only statement is not a fallback for a required durable write.

Keep fallback discovery bounded to the relevant action surface. Declare a blocker only after valid authorized paths are reasonably exhausted, and name the residual blocker accurately.

## State changes

Do not invent new workflow mechanisms, coordination state, or authority boundaries without explicit approval. Record dependencies and blockers instead of silently adapting process.

## Evidence

Do not claim validation, merge, deployment, or runtime state without authoritative evidence. Follow the assigned role contract for required evidence and handoff.

## Development Workflow Friction capture

Repository-modifying roles must be able to discover `Dish — Development Workflow Friction` (`1217443500915644`) without Marco naming it. When non-blocking development-process friction appears: **notice -> dedupe -> log/update -> continue**. Search the Friction project first; update a matching item or create an unprioritized `Inbox` item with what/where, why it matters, evidence/reproduction, role/host, and suggested next action; then continue the assigned scope.

Do not manufacture urgency from repetition, age, or annoyance. A blocker required to complete the active task stays on the active task/PR rather than becoming a parallel Friction task. The Friction project is a capture/triage surface, not a second orchestration authority.

## Code-smell / engineering-debt logging

When code-touching work exposes material non-blocking engineering debt, use `Dish — Code Smells / Engineering Debt` (`1217443501022227`) as a capture surface, not a second execution authority. Check for a matching item first; update it when present, otherwise create one unprioritized `Inbox` item with affected path/component, issue, why it matters, concrete evidence/example, and suggested next action; then continue the assigned scope.

Do not opportunistically fix unrelated debt, inflate priority, or move a blocker required for the active task away from its active task/PR. Current blockers remain on the active work surface.

These two capture surfaces are legacy triage queues. The dedupe-first matching-item update / unprioritized `Inbox` create operations authorized above are bounded non-V2 capture writes; `asana-v2-project-mode.md` unregistered-project rules forbid applying V2 lifecycle semantics there, not these exact standing capture operations. This grants no broader task movement, priority, assignment, dispatch, or execution authority.

Development Workflow owns recurring queue hygiene: include both `Inbox` queues in fresh-start, re-grounding, status, and explicit triage sweeps; dedupe against active owning work; move only actually-triaged items to `Triaged` and completed fixes to `Done`. This is triage/routing ownership, not semantic implementation authority.
