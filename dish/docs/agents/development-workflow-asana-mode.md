# Development Workflow Asana project mode

This is the shared stage-1 compatibility contract for governed writes to the canonical
Development Workflow Asana project. It applies to every standing role and Worker mode that may
create, move, update, or complete a task in that project. Read-only inspection may continue when
a mutation is refused.

## Exact mode classification

First require project GID `1217419962189616`; a matching name on another project is not authority.
Then read the project name and complete section list from Asana in the same mutation flow. Match
names exactly, without case folding, prefix matching, or substring/version guessing:

- `Dish — Development Workflow` is **LEGACY** only when all legacy lifecycle sections exist:
  `Backlog`, `Ready`, `In Progress`, `Review / Integration`, `Done`, and `Blocked / Decision`.
  A service-created non-lifecycle section such as `Untitled section` may coexist. Any V2-only
  lifecycle section makes the state contradictory.
- `Dish — Development Workflow v2` is **V2** only when all V2 lifecycle sections exist:
  `Needs Processing`, `Needs Research`, `Needs Agentic Review`, `Needs Human Review`,
  `Waiting on Dependency`, `Ready`, `Under Development`, and `Done`. No legacy-only lifecycle
  section may remain.
- `Dish — Development Workflow v3` is **V3-UNSUPPORTED** under this generation.
- Any other name, including another version such as `v4`, is **UNKNOWN**.

Missing sections, both-generation lifecycle sections, an unreadable complete section list, or a
name/structure mismatch is **CONTRADICTORY**. Do not infer mode from an individual task membership.

## Mutation behavior

Immediately before a governed Development Workflow Asana mutation, classify the exact live project:

- LEGACY permits only the existing lifecycle and never creates V2 sections opportunistically.
- V2 permits only the V2 lifecycle and never recreates legacy sections.
- V3-UNSUPPORTED performs zero governed mutation and returns
  `PROJECT MODE V3 REQUIRES UPDATED PROJECT SETTINGS / GPT ACTION PROTOCOL`.
- UNKNOWN or CONTRADICTORY performs zero governed mutation and reports the exact observed name and
  structural mismatch; it never repairs, renames, or chooses a generation by guessing.

The stage-1 structural cutover preserves the project GID and keeps the unversioned name until the
V2 structure and task migration have been written and read back successfully. Rename to exact
`Dish — Development Workflow v2` only as the final mode-activation write, then read back the name,
sections, fields, and affected task memberships. A failed pre-rename migration remains visibly
LEGACY or contradictory rather than advertising V2. A failed final rename remains recoverable by
reconciliation against the captured migration snapshot; do not invent a permanent migration
lifecycle section.

## Project-settings rollout

Repository grounding does not prove an already-running ChatGPT session adopted this mutation
protocol. Before stage-1 structural cutover, update the installed Coordinator, Development
Workflow, all standing role Projects that can write owning-task lifecycle state, and the manual
Worker Project profile to the generated settings containing this contract. Replace/restart active
Asana-writing sessions, unless Marco gives an explicit current-session override, and verify a fresh
Coordinator/Development Workflow/Worker session classifies the still-unversioned project as LEGACY.
After cutover, verify fresh sessions classify the renamed project as V2. Pre-stage-1 settings are
not safe migration participants.
