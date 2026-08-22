# Development Workflow authority and state

## Read this when

Read this when adding or changing a development-workflow fact, writer, projection, identity, or reconciliation rule.

## Scope

This document identifies authoritative facts and derived state. Detailed transitions belong in the lifecycle and role contracts.

## Current architecture

| Fact | Authority | Derived consumers |
|---|---|---|
| Repository source/history and branch/PR head | GitHub | local refs, bundles, dispatcher views |
| Formal PR Review and CI/check evidence | GitHub records bound to exact head | Integration gate, status projection |
| Accepted design, task decisions, lifecycle section, and dependencies | Live owning Asana task/project | handoffs and PR context |
| Role authority and process invariants | Current repository contracts | Project kernels and handoffs |
| Actual TEST/PROD state | Direct runtime/environment evidence | Asana status summaries |
| Local Implementation ownership | Repository worktree claim for the exact lineage | identity files and PR leases |
| Local Integration admission | Per-PR/head fence and fresh GitHub/Asana reads | dispatcher status |

[The lifecycle dispatcher](../../../../scripts/pr_lifecycle.py) reconstructs a queue view from durable GitHub and linked Asana facts. Its process memory and output are projections, not authoritative lifecycle storage.

## Invariants

- A projection never silently overrides its owning authority.
- Branch names and PR numbers are insufficient when the contract requires an exact head or lineage.
- Authenticated account metadata does not prove Marco personally decided or authored an action.
- Mutable current task state does not retroactively redefine an already-dispatched exact design or PR candidate without explicit lineage movement.
- Ambiguous writes are reconciled by authoritative readback before replay.
- Contradictions remain explicit until the owning authority resolves them.

## Current anchors

- [`../../agents/operator-provenance.md`](../../agents/operator-provenance.md)
- [`../../agents/development-workflow-asana-mode.md`](../../agents/development-workflow-asana-mode.md)
- [`../../../../scripts/pr_lifecycle.py`](../../../../scripts/pr_lifecycle.py)
- [`../../../../tools/agent-worktree`](../../../../tools/agent-worktree)

## Related documents

- [Lifecycle](lifecycle.md)
- [Work identity and concurrency](work-identity-and-concurrency.md)
- [Recovery, observability, and completion](recovery-observability-and-completion.md)
