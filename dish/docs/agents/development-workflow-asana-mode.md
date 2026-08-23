# Development Workflow Asana project mode

This is the shared operating contract for governed writes to the canonical Development Workflow
Asana project. It applies to every standing role and Worker mode that may create, move, update, or
complete a task there. Read-only inspection may continue when a mutation is refused.

Immediately before each governed write, freshly read this contract and the exact live project
name, complete section list, fields, task, and relevant chronological evidence. An earlier session
read or repository grounding does not prove that a running agent has the current V2 semantics.

## Exact mode classification

First require project GID `1217419962189616`; a matching name on another project is not authority.
Match names and section names exactly, without case folding, prefix matching, or version guessing:

- `Dish — Development Workflow` is **LEGACY** only when all legacy lifecycle sections exist:
  `Backlog`, `Ready`, `In Progress`, `Review / Integration`, `Done`, and `Blocked / Decision`.
  A service-created non-lifecycle section such as `Untitled section` may coexist. Any V2-only
  lifecycle section makes the state contradictory.
- `Dish — Development Workflow v2` is **V2**. Exact GID/name establishes the lifecycle generation;
  the nine canonical sections are `Needs Processing`, `Needs Research`, `Needs Agentic Review`,
  `Needs Human Review`, `Waiting on Dependency`, `Ready`, `Under Development`,
  `Needs Post-Merge Rollout`, and `Done`.
- `Dish — Development Workflow v3` is **V3-UNSUPPORTED** under this generation.
- Any other name, including another version such as `v4`, is **UNKNOWN**.

Never infer mode from one task's membership. After V2 identity is established, structural health is
**V2_COMPLETE** when all nine sections exist exactly once with readable required fields;
**V2_DEGRADED_KNOWN** when known sections/fields are missing but all present meanings are unique,
unambiguous, and free of legacy/V2 collision; or **V2_CONTRADICTORY** for duplicate, mixed,
unreadable, or otherwise unsafe meanings.

LEGACY permits only its existing lifecycle and never creates V2 sections opportunistically. V2
permits only the V2 lifecycle and never recreates legacy sections. V3-UNSUPPORTED performs zero
governed mutation and returns
`PROJECT MODE V3 REQUIRES UPDATED PROJECT SETTINGS / GPT ACTION PROTOCOL`. UNKNOWN performs zero
governed mutation and reports the exact mismatch without selecting a generation by guesswork.

`V2_DEGRADED_KNOWN` never bricks the project. Operations using only present valid structure,
including comments and unrelated task/field writes, continue under normal role authority. When the
requested operation needs missing structure, deterministically repair it only if the acting
role/tool owns that exact repair and the canonical schema is fully known; authoritatively reread
the complete structure before continuing. Missing custom-field structure is repairable only when
its exact type/options are canonical and supported. Otherwise block only that operation and surface
one repair owner/debt. `V2_CONTRADICTORY` likewise fails closed only the operation whose lifecycle
or field meaning is unsafe; unrelated safe work continues.

## V2 lifecycle meanings

- **Needs Processing** — raw captured intake that has not been semantically triaged. It is not a
  generic uncertainty bucket and does not imply research.
- **Needs Research** — material facts, mechanism, design, ownership, feasibility, evidence, or
  scope remain unresolved. Leave when the next required step is concrete.
- **Needs Agentic Review** — a concrete research/design/result exists and independent agentic
  review or re-review is next. This is pre-development semantic review, not GitHub PR Review.
- **Needs Human Review** — the next required step is a genuine, task-specific Marco or other
  authorized-human decision supported by durable authority. Uncertainty and severity are not gates.
  A semantic Review-governance change remains here after exact independent Design Review until
  durable evidence binds Marco approval to the exact current unsuperseded generation and material
  delta. Routine code review, mechanical semantic-parity work, and repairable metadata do not
  acquire this gate merely because they touch Review surfaces.
- **Waiting on Dependency** — a named unresolved external, cross-task, or system dependency blocks
  progress; record its owner, wake condition, and what follows.
- **Ready** — current evidence affirmatively proves the task is dispatchable now.
- **Under Development** — authorized execution has actually accepted or begun the work. A prepared
  handoff, section move, branch, or comment alone does not prove execution.
- **Needs Post-Merge Rollout** — source has landed but task-specific rollout, activation, runtime,
  or operator acceptance remains open.
- **Done** — the objective is genuinely satisfied or durably resolved with no residual
  responsibility. `completed=true` is the completion fact; section placement is presentation.

Detailed PR, CI, Review, and merge state remains GitHub/lifecycle truth rather than extra Asana
execution state. Runtime and deployment evidence remains separate when operational completion
depends on it.

## Chronological authority and Ready safety

Before classification or consequential movement, reconstruct current state from current notes,
chronological task stories/comments, current GitHub evidence where repository work exists, and
runtime/deployment evidence where operational completion matters. Decisions, corrections, review
outcomes, holds/releases, dependency changes, ownership transfer/folding, dispatch, merge, rollout,
and completion evidence are material. Later authoritative evidence supersedes contradictory older
lifecycle wording; keyword matches, titles, comments, stale section placement, and authenticated
account attribution do not independently establish state.

Ready requires all of the following to be current: concrete scope/outcome; required research and
design complete; required independent pre-development review satisfied; required human decision
resolved; no hold or prohibition; no unresolved dependency; no later supersession/folding; and no
evidence that work is already active, source-landed with residual rollout, or complete. An old PASS,
an audit `BLOCKER`, historical Ready placement, or self-declared `IMPLEMENTATION READY` is
insufficient. If these facts cannot be proved, route to the actual next step instead of guessing
Ready.

Chronological evidence determines current orchestration state. It does not silently rewrite the
exact accepted-generation or exact-head lineage of an already-authorized Implementation or Review
candidate; reconcile that candidate under its owning identity contract.

## Structured mutation and reconciliation

Sections and custom fields are the structured orchestration projection. When authorized lifecycle
or field state changes, update the structured value itself and authoritatively read it back.
Comments preserve decisions, review evidence, corrections, provenance, and discussion; a comment
saying `Ready`, `P0`, or `waiting` is never a substitute for the corresponding structured write.

During migration, live structured values may contradict current durable authority. When the correct
state is mechanically clear and the acting role owns the repair, make the smallest structured
correction and verify section, field, completion, and task identity by readback. When the correct
state is unclear, classify it as `RECONCILIATION_REQUIRED`, leave the task unchanged, and surface
the bounded ambiguity to the reconciliation owner. `RECONCILIATION_REQUIRED` is an outcome, not a
section, and ambiguity is not automatically a human decision.

If a write response is ambiguous, reconcile current state before any retry that could duplicate or
compound the effect. Never represent a prepared comment, handoff, or intended move as a completed
structured mutation.

## Custom fields and role boundaries

- **Priority** comes only from explicit Marco/durable priority authority or an approved
  deterministic rule. Otherwise use `UNSET`; severity words do not imply priority.
- **Code Area**, **Agent owner**, **Version**, and **Has Headline** follow the shared
  [`structured task-field lifecycle`](structured-task-fields.md). Freshly read it before changing
  one of those fields. Their values project durable authority and never create it.

Coordinator/Development Workflow own semantic cross-workstream triage, dedupe, consolidation,
reconciliation, and routing. Finding producers normally create raw findings in Needs Processing
unless their standing contract authorizes deeper classification. An owning specialist/Coordinator
may move pre-development tasks only when the destination rule is satisfied. Implementation,
Review, and Integration do not move tasks merely to mirror GitHub substates. Tool capability never
grants role authority. Do not invent project-wide human, Review, dependency, or readiness gates.

## Project-settings rollout

New or restarted ChatGPT sessions receive this rule through the generated current Project kernel.
Local Claude/Codex roles and manual Worker read it through the role index/Worker bootstrap. A
running session with legacy or unproved cached semantics must freshly reload this exact current
contract before a write; if it cannot, replace/restart it unless Marco gives an explicit scoped
current-session override. Repository re-grounding alone is not proof that installed Project
settings or already-loaded context changed.

The earlier structural-cutover rule remains historical safety: pre-stage-1 settings are not safe
V2 writers, and a session still subject to that breaking generation must complete its approved
Project-settings resynchronization before this fresh-read rule can apply.
