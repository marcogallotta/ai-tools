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
| Structured task fields (`Code Area`, `Agent owner`, `Version`, `Has Headline`) | Their underlying current scope, role, generation, or exact human-intent evidence | Asana field values |
| Role authority and process invariants | Current repository contracts | Project kernels and handoffs |
| Standing gate-exception policy and exact activation evidence | Current reviewed Git registry; Marco decision and urgent debt task remain live Asana authority | Grounded agents and per-use lifecycle records |
| Actual TEST/PROD state | Direct runtime/environment evidence | Asana status summaries |
| Local Implementation ownership | Repository worktree claim for the exact lineage | identity files and PR leases |
| Local Integration admission | Per-PR/head fence and fresh GitHub/Asana reads | dispatcher status |
| Coordinator current-state/action frontier | GitHub, Asana, repository policy, and direct runtime evidence by fact | normalized lifecycle/task projection and deterministic hard-invariant admission |

[The lifecycle dispatcher](../../../../scripts/pr_lifecycle.py) reconstructs a queue view from durable GitHub and linked Asana facts. Its process memory and output are projections, not authoritative lifecycle storage.

The Coordinator normally consumes that maintained normalized projection rather than asking a model to rediscover mechanically knowable state. The deterministic layer constructs the complete eligible frontier and owns exact identity, contradiction/unknown classification, recorded dependencies, current controls, execution truth, wake/receipt/fence identity, and hard-invariant admission. Model judgment may choose among already valid actions and interpret leverage, convergence, local-benefit, and operator-attention tradeoffs; it cannot mint facts or authority. Missing projection facts remain unknown/reconciliation-required, and bounded direct reads are recovery/forensic fallback.

## Invariants

- A projection never silently overrides its owning authority.
- Exact registered Asana project identity and structural health are separate facts. A known missing
  canonical capability degrades only operations that require it; present unambiguous capabilities
  remain usable. Exact owned repair requires complete canonical schema plus authoritative readback.
  Unknown generations, conflicting meanings, and unreadable authority still fail closed at the
  affected boundary.
- Standing gate exceptions share repository-policy freshness; Project settings are not a second
  exception authority. Every new activation is invalid until current Git binds exact gate semantics
  and Marco provenance to an authoritatively read-back same-day P-CRITICAL Development Workflow
  follow-up.
- Blank structured fields can truthfully mean raw, unassessed, legacy, or not-yet-adopted state;
  they are reconciled at natural lifecycle checkpoints rather than through a blanket metadata gate.
- `Has Headline: Yes - approved` projects recoverable exact words plus explicit Marco approval. It
  is sticky until a new explicit durable Marco decision replaces it; the field cannot mint or
  rewrite human intent.
- Branch names and PR numbers are insufficient when the contract requires an exact head or lineage.
- Authenticated account metadata does not prove Marco personally decided or authored an action.
- Mutable current task state does not retroactively redefine an already-dispatched exact design or PR candidate without explicit lineage movement.
- Ambiguous writes are reconciled by authoritative readback before replay.
- Contradictions remain explicit until the owning authority resolves them.
- A Coordinator proposal remains advisory until deterministic admission revalidates its exact current facts and hard invariants.
- Derived Coordinator state creates no queue, dependency authority, scheduler, dispatch authority, or prompt-side state mirror.

## Current anchors

- [`../../agents/operator-provenance.md`](../../agents/operator-provenance.md)
- [`../../agents/development-workflow-asana-mode.md`](../../agents/development-workflow-asana-mode.md)
- [`../../agents/structured-task-fields.md`](../../agents/structured-task-fields.md)
- [`../../../../scripts/pr_lifecycle.py`](../../../../scripts/pr_lifecycle.py)
- [`../../../../tools/agent-worktree`](../../../../tools/agent-worktree)

## Related documents

- [Lifecycle](lifecycle.md)
- [Work identity and concurrency](work-identity-and-concurrency.md)
- [Recovery, observability, and completion](recovery-observability-and-completion.md)
