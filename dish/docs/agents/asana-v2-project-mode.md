# Asana V2 project mode (shared, project-agnostic)

This is the shared operating contract for the V2 Asana lifecycle across every governed Dish
project. It applies to every standing role and Worker mode that may create, move, update, or
complete a task in a project registered below. Read-only inspection may continue when a mutation
is refused.

This doc does not replace [`development-workflow-asana-mode.md`](development-workflow-asana-mode.md).
That file remains the standalone, unmodified authority that existing ChatGPT Development Workflow
sessions already read directly; it stays in force until those sessions are confirmed replaced and a
separate follow-up task retires or forwards it. This doc is the authority for every other governed
project, and the shared reference point for the general V2 mechanics.

Immediately before each governed write, freshly read this contract, the project's exact current
registry row, and the exact live project name, complete section list, fields, task, and relevant
chronological evidence. An earlier session read or repository grounding does not prove that a
running agent has current semantics.

## Project registry

Every Dish-prefixed Asana project must have an explicit row here before any agent applies V2
semantics to it. A project not listed is **unregistered**: an agent must explicitly refuse governed
V2 mutation and say so, rather than silently ignoring the project or silently applying V2 rules to
it because the name looks similar.

A registry row is only a GID-to-base-name mapping. It records no generation, migration progress, or
mutation-authority state of its own — classification comes only from the live project name, read
fresh every time (see below). Nothing here is a cached or repository-driven authority.

| Project GID | Base name |
|---|---|
| `1217419962189616` | Dish — Development Workflow |
| `1217404747383060` | Dish — PostgreSQL / Dark Launch |
| `1217382473444945` | Dish — Coordinator |

### Reference only: other real Dish project shapes (not registered, not migrated)

These are recorded so a future migration of any of them is a registry addition, not a redesign.
Nothing here migrates them or applies V2 semantics to them.

- **Triage-queue shape** (`Inbox` / `In Progress` / `Triaged` / `Done`): Dish — Development Workflow
  Friction, Dish — Code Smells / Engineering Debt.
- **Legacy pre-V2 shape** (`Backlog` / `Ready` / `In Progress` / `Review / Integration` /
  `Blocked / Decision` / `Done`): Dish — Workflow.
- **Telemetry-log shape**: Dish — Agent Performance.
- **Unstructured** (only `Untitled section`): Dish — Tests, Dish — Agentic Docs & Agent Behavior.

### Bounded legacy capture writes

`Dish — Development Workflow Friction` (`1217443500915644`) and `Dish — Code Smells / Engineering Debt` (`1217443501022227`) remain unregistered for V2 lifecycle semantics. Their exact contributor-base `notice -> dedupe -> log/update -> continue` contracts nevertheless authorize the bounded legacy capture operations they name: update a matching finding or create an unprioritized `Inbox` finding. These are non-V2 capture writes, not V2 mutation. Do not apply V2 sections/fields, infer priority, move or dispatch work under this shared capture exception, or generalize the exception to another unregistered project. Development Workflow's standing role contract separately authorizes the named Friction `Inbox` triage and moves; this shared exception neither revokes that role-specific authority nor extends it to Code Smells.

## Exact mode classification

Require the project GID from the registry row; a matching name on another project is not
authority. Classification comes only from the live project name, read fresh on every governed
action — never from a prior session's memory, a doc note, or any other stored/repository-driven
setting. Match names and section names exactly, without case folding, prefix matching, or guessing.

For a registered row's base name, the live project name is one of exactly three shapes:

- **Live name equals the base name, with no version suffix** — **LEGACY**. Zero governed V2
  mutation.
- **Live name equals `<base name> v2`** — **V2**, subject to the section-signature check below: all
  nine V2 lifecycle sections must exist and no legacy-only section may remain. A match is full V2
  mutation authority; a mismatch is **CONTRADICTORY** — zero mutation, report the exact mismatch
  without repairing, renaming, or guessing.
- **Live name equals `<base name> v` followed by anything other than `2`** (`v3`, `v4`, or any
  other value) — **STOP**. Zero governed mutation. Explicitly flag it to Marco and ask what to do.
  Do not guess the new generation's rules and do not fall back to applying V2 rules to it.

Any other live name (malformed suffix, or not matching the base name at all) is **UNKNOWN**: zero
governed mutation, report the exact mismatch without repairing, renaming, or guessing.

## V2 lifecycle meanings

The full V2 lifecycle is nine sections: `Needs Processing`, `Needs Research`, `Needs Agentic
Review`, `Needs Human Review`, `Waiting on Dependency`, `Ready`, `Under Development`,
`Needs Post-Merge Rollout`, `Done`.

- **Needs Processing** — raw captured intake that has not been semantically triaged. Not a generic
  uncertainty bucket and does not imply research.
- **Needs Research** — material facts, mechanism, design, ownership, feasibility, evidence, or
  scope remain unresolved. Leave when the next required step is concrete.
- **Needs Agentic Review** — a concrete research/design/result exists and independent agentic
  review or re-review is next. This is pre-development semantic review, not GitHub PR Review.
- **Needs Human Review** — the next required step is a genuine, task-specific Marco or other
  authorized-human decision supported by durable authority. Uncertainty and severity are not gates.
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

New or restarted ChatGPT sessions receive the applicable rule through the generated current Project
kernel. Local Claude/Codex roles and manual Worker read it through the role index/Worker bootstrap.
A running session with legacy or unproved cached semantics must freshly reload the exact current
applicable contract before a write; if it cannot, replace/restart it unless Marco gives an explicit
scoped current-session override. Repository re-grounding alone is not proof that installed Project
settings or already-loaded context changed.
