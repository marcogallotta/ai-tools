# Agent-process improvement TODO

These are explicitly **not adopted policy yet**. They are retained so they survive coordinator replacement and can be researched/discussed before changing the standing contracts.

## Review / CI

- Investigate an explicit review-latency/SLA policy and when central review should be forked because the coordinator is becoming the bottleneck.
- Investigate moving more deterministic/low-value review work into automation.
- Investigate required-vs-informational checks, especially asynchronous post-merge native PostgreSQL CI so slow native certification is not automatically on the merge-critical path. Define how unresolved post-merge failures are carried into later review/coordination.
- Investigate empirical feedback from review/audit outcomes: which review classes actually catch merge-worthy defects, which audit findings should have blocked originally, and how routing should adapt.

## Audit layer

- Define audit cadence, number/parallelism of independent audit agents, milestone/time backstops, and exact escalation rules for when an audit finding interrupts current work versus becoming scheduled follow-up.
- Preserve the distinction that an audit finding on an older baseline does not automatically block a newer pending merge.

## Patch shape and task discipline

- Research/adopt a patch-size/decomposition policy: large implementation tasks may need an ordered stack of smaller independently reviewable patches/commits rather than one massive diff.
- Research/adopt an early wrong-problem/scope-creep gate so agents solve the requested problem and defer unnecessary expansion.
- Research/adopt an information-budget rule for agent returns: omit dead information that changes neither Marco's next decision/action nor durable project state.

## Human and specialist roles

- Refine the small set of situations that genuinely require Marco's explicit human judgment; keep those escalations focused on the highest-value decision.
- Investigate durable coordinator-like standing contracts for recurring specialist agents (workflow, frontend, PostgreSQL, release/cutover, etc.) so task handoffs carry live delta rather than repeated stable instructions.

## Workflow / PostgreSQL cutover discussion

- Before declaring the PostgreSQL-authoritative / no-Asana workflow ready for real use, discuss with the workflow agent the remaining workflow-side cutover gaps: archiving a Dish, commenting on a Dish, and a viable cooking-agent takeover strategy under the connected-GPT instruction budget. First investigate whether the connected/operator and cooking instructions can be safely consolidated; if not, evaluate a separate cooking GPT/configuration as a temporary operational design.
- Also discuss a preferred lightweight path for the cooking agent to propose or write factual updates about what was actually cooked (for example ingredient substitutions, quantity changes, or cooking notes/analysis).
- **Do not encode the verification/authority boundary for those cooking updates yet.** That boundary is an open design question to work through with the workflow agent; do not substitute a coordinator-authored judgment call.

## Live coordination / Asana / Git

- Design a shared live-development control plane using Asana rather than coordinator-local drift files: queued/proposed work, in-flight work, exact working HEAD/base, expected file/semantic overlap, review state, audit state, blockers, missing certification, and completion history should be visible to agents and Marco.
- Explore specialist-role projects plus one shared execution project, including multi-homing so specialist backlogs can feed a single global queue without duplicating state.
- Define safe task state transitions/claiming rules and the minimum task metadata needed for a replacement agent to take over directly from repository + Asana without a bespoke handoff bundle.
- Explore using GitHub/remote Git as the normal code-artifact transport: Asana task -> exact base/work state -> agent branch/commit/PR -> exact commit/PR identity written back to Asana -> reviewer/merge flow. Prefer Git identities over anonymous patch-file transport when feasible.
- Verify the practical GitHub connector/tooling path available to coordinator/reviewer/implementation agents before adopting it.
- The current Asana connector can list/read attachment metadata/URLs but does not expose attachment upload. Investigate whether attachment upload needs a different integration; do not assume Asana itself lacks API support.
- Keep Git/repository as durable code/process/architecture truth. Treat Asana as candidate live orchestration truth only after the coordination design is reviewed and adopted.
- Reassess whether `LIVE_DELTA.md` is still needed as anything more than an emergency fallback once Asana-backed live state is reliable.

## Audit design follow-up

- Research a stage-sensitive audit escalation model. The same defect may have different urgency in dark launch versus after PostgreSQL becomes sole authority; interruption should depend on the current development/authority stage and actual consequence, not severity labels in isolation.
- Reassess whether a fixed time backstop is warranted for this single-driver/agent workflow. Prefer evidence-based cadence combining accumulated-risk/milestone triggers with a backstop only if it prevents meaningful assurance gaps without creating excessive interruption.

Do not silently implement these TODOs. Discuss/research them and update standing policy only after the decision is clear.
