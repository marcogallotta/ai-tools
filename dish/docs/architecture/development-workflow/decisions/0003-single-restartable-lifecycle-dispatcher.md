# ADR 0003: The lifecycle dispatcher is restartable derived orchestration

Status: Accepted

## Read this when

Read this when proposing another PR queue, poller, dispatcher database, lifecycle controller, or chat-owned routing loop.

## Context

Routine PR observation and routing must survive agent/session loss without making Marco poll or ferry transcripts. Multiple independent controllers would race and disagree.

## Decision

[The lifecycle dispatcher](../../../../../scripts/pr_lifecycle.py) is the single repository-owned lifecycle dispatcher. It reconstructs state from current GitHub and linked Asana facts on restart, reuses shared gate predicates, and stores no authoritative queue database.

## Consequences

Extensions add derived classifications or supported consumers to the existing dispatcher. Process memory, rendered tables, and leases remain projections/visibility rather than authority.

## Related documents

- [Recovery, observability, and completion](../recovery-observability-and-completion.md)
- [Authority and state](../authority-and-state.md)
