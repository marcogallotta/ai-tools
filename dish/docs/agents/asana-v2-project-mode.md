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

`lifecycle_state` has exactly three stored values. There is no fourth stored value for
"unregistered" — that is the absence of a row, not a state.

| Project GID | Project name | `lifecycle_state` | Notes |
|---|---|---|---|
| `1217419962189616` | Dish — Development Workflow v2 | `v2-governed` | Governed under `development-workflow-asana-mode.md` directly; this doc's general mechanics describe the same rules but that file remains the binding text for this project. |
| `1217404747383060` | Dish — PostgreSQL / Dark Launch v2 | `structural-repair-pending` | Current signature: 8 of 9 V2 sections present, missing `Needs Post-Merge Rollout`. Zero ordinary V2 mutation authority until the sequenced structural-repair operation (below) reads back 9-of-9 and flips this row. |
| `1217382473444945` | Dish — Coordinator | `migration-pending` | Current signature: legacy sections `Backlog`, `Ready`, `In Progress`, `Review / Integration`, `Blocked / Decision`, `Done`, plus `Untitled section`; no V2 custom fields. Zero V2 mutation authority until a full future migration is durably recorded here. |

### `lifecycle_state` meanings

- **`v2-governed`** — full V2 mutation authority under this contract. The project's exact current
  section-name signature must match the full 9-section V2 lifecycle below; a mismatch is
  `CONTRADICTORY` and refuses governed mutation rather than repairing, renaming, or guessing.
- **`structural-repair-pending`** / **`migration-pending`** — registry membership only, zero
  ordinary V2 task-mutation authority. This contract records the project's exact current section
  signature and requires that signature still match before any read/report action. Moving out of
  either state happens only through an explicit, separately-authorized one-shot project-structure
  migration operation (never ordinary task mutation): it verifies the project's exact starting
  signature as a precondition, performs the structural change, authoritatively reads back the
  resulting 9-section V2 signature, and only then durably flips that row to `v2-governed` in this
  file. Until that flip is recorded here, the project stays zero-authority regardless of how close
  its live structure looks to complete.
- **Unregistered** (no row) — out of scope by default. Refuse governed V2 mutation explicitly.

### Sequenced follow-up: PostgreSQL / Dark Launch structural repair

Not part of the change that introduces this doc. Once this doc and its kernel/test wiring are
merged and authoritative on `main`, an explicitly authorized follow-up verifies project
`1217404747383060`'s exact 8-section precondition, adds the missing `Needs Post-Merge Rollout`
section, authoritatively reads back the full 9-section structure, and durably flips its row above to
`v2-governed`. Until that follow-up lands and this file is updated, treat the project as
`structural-repair-pending`.

### Reference only: other real Dish project shapes (not registered, not migrated)

These are recorded so a future migration of any of them is a registry addition, not a redesign.
Nothing here migrates them or applies V2 semantics to them.

- **Triage-queue shape** (`Inbox` / `In Progress` / `Triaged` / `Done`): Dish — Development Workflow
  Friction, Dish — Code Smells / Engineering Debt.
- **Legacy pre-V2 shape** (`Backlog` / `Ready` / `In Progress` / `Review / Integration` /
  `Blocked / Decision` / `Done`): Dish — Workflow.
- **Telemetry-log shape**: Dish — Agent Performance.
- **Unstructured** (only `Untitled section`): Dish — Tests, Dish — Agentic Docs & Agent Behavior.

## Exact mode classification

Require the project GID from the registry row; a matching name on another project is not
authority. Match names and section names exactly, without case folding, prefix matching, or version
guessing.

For a `v2-governed` row, the project is **V2** only when all V2 lifecycle sections exist (see
below) and no legacy-only lifecycle section remains. Missing sections, mixed-generation lifecycle
sections, an unreadable complete section list, or a name/structure mismatch against the registry
signature is **CONTRADICTORY**: perform zero governed mutation and report the exact mismatch
without repairing, renaming, or selecting a generation by guesswork.

For a `structural-repair-pending` or `migration-pending` row, the project performs zero governed V2
mutation by definition. Confirm the recorded signature still matches on any read/report action; a
signature that no longer matches is itself worth surfacing, since it means live state has drifted
from what this file records, but it does not by itself grant mutation authority — only the
authorized structural-migration operation and a recorded registry flip do that.

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
- **Code Area** is coarse routing/parallelism context derived from the actual work surface. It is
  not conflict proof or mutation authority.
- **Version** is populated only when durable evidence explicitly identifies this task's own
  version/generation. Context such as `Review V2` or `Integration V1` is not a field value and
  Version never determines readiness.

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
