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

## PR/change shape and task discipline

- Research/adopt a PR/change decomposition policy: large implementation tasks may need ordered smaller independently reviewable PRs, or coherent commits within one PR, rather than one massive diff.
- Research/adopt an early wrong-problem/scope-creep gate so agents solve the requested problem and defer unnecessary expansion.
- Research/adopt an information-budget rule for agent returns: omit dead information that changes neither Marco's next decision/action nor durable project state.

## Human and specialist roles

- Refine the small set of situations that genuinely require Marco's explicit human judgment; keep those escalations focused on the highest-value decision.
- Extend the adopted standing-contract pattern deliberately when another recurring specialist role justifies it. Workflow, PostgreSQL / Dark Launch, and Development Workflow already have standing specialist lanes.

## Workflow / PostgreSQL cutover discussion

- Before declaring the PostgreSQL-authoritative / no-Asana workflow ready for real use, discuss with the workflow agent the remaining workflow-side cutover gaps: archiving a Dish, commenting on a Dish, and a viable cooking-agent takeover strategy under the connected-GPT instruction budget. First investigate whether the connected/operator and cooking instructions can be safely consolidated; if not, evaluate a separate cooking GPT/configuration as a temporary operational design.
- Also discuss a preferred lightweight path for the cooking agent to propose or write factual updates about what was actually cooked (for example ingredient substitutions, quantity changes, or cooking notes/analysis).
- **Do not encode the verification/authority boundary for those cooking updates yet.** That boundary is an open design question to work through with the workflow agent; do not substitute a coordinator-authored judgment call.

## Live coordination / Asana / Git

- Current adopted coordination lanes include `Dish — Coordinator`, `Dish — Workflow`, `Dish — PostgreSQL / Dark Launch`, and `Dish — Development Workflow`. Validate that replacement coordinators/specialists can take over from repository + their adopted Asana projects without conversation reconstruction before expanding the specialist-project pattern further.
- Define safe claiming/concurrency before multiple autonomous agents can independently claim the same `Ready` work; section movement alone is not an atomic claim mechanism.
- Investigate moving concurrent local Codex/Claude implementation and investigation work out of one shared `main` checkout and into isolated Git worktrees, including creation/cleanup ownership, exact-base identity, integration, and collision rules.
- Investigate a first-class read-only way for agents to determine the exact code/schema/config/runtime currently deployed in TEST and production. GitHub source/history and Asana coordination state must not be treated as proof of deployed state.
- GitHub is adopted source/history authority. Keep exact Git identities in Asana task state when relevant; do not make Asana attachments a second code-artifact transport.
- Use specialist projects rather than a shared global execution mirror for coordinator visibility. Revisit multi-homing only for genuinely cross-area work and avoid any design that requires duplicate lifecycle synchronization.
- Reassess whether `LIVE_DELTA.md` is still needed as anything more than an emergency/unadopted-lane fallback once the Asana pilot is proven reliable.

## Audit design follow-up

- Research a stage-sensitive audit escalation model. The same defect may have different urgency in dark launch versus after PostgreSQL becomes sole authority; interruption should depend on the current development/authority stage and actual consequence, not severity labels in isolation.
- Reassess whether a fixed time backstop is warranted for this single-driver/agent workflow. Prefer evidence-based cadence combining accumulated-risk/milestone triggers with a backstop only if it prevents meaningful assurance gaps without creating excessive interruption.

Do not silently implement these TODOs. Discuss/research them and update standing policy only after the decision is clear.
