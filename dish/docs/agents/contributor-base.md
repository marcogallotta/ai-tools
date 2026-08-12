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

## State changes

Do not invent new workflow mechanisms, coordination state, or authority boundaries without explicit approval. Record dependencies and blockers instead of silently adapting process.

## Evidence

Do not claim validation, merge, deployment, or runtime state without authoritative evidence. Follow the assigned role contract for required evidence and handoff.
