# Dish private frontend (future work)

**DRAFT — not ready for implementation.** This is preserved future design only: not an approved
interaction model, persistence schema, product commitment, or implementation authorization. It has
not been re-reviewed since its original drafting and must not be treated as an accepted spec. It
was originally part of the single database-backend design doc and
was removed during a later editing pass without a recorded rationale; it is restored here as its
own document because [`future.md`](future.md) already lists a database-backed store with "a
separate frontend" as a near-term candidate, and this is the detailed shape that proposal referred
to. Read [`future.md`](future.md) first for how this fits alongside other future-work items, and
[`database-backend.md`](database-backend.md) (with its companions
[`database-backend-imp.md`](database-backend-imp.md) and
[`database-backend-migration.md`](database-backend-migration.md)) for the Stage A authority
migration this frontend would eventually sit in front of. Stage A explicitly excludes this frontend
as a prerequisite; nothing here authorizes building it now.

Any future implementation still needs its own design pass, especially for mutation stages where a
visual gesture could conceal a governed transition.

## Delivery stages

The frontend should be delivered incrementally. The stages below are a product direction, not an
approved interaction model or persistence schema. Each mutation stage needs separate design work
before implementation, especially where a visual gesture could conceal a governed transition.

### Stage 1: reading and discovery

- list tasks and open the exact current authoritative version and its rendered view;
- show basic search and filters by title, location, completion, and active-operation status;
- filter by a dish's destination category independent of its current queue placement, so a dish in
  Research or Verification Queue remains findable by where it's headed;
- show content, location, operation, Verification, audit, and recovery history;
- render structured dish fields and exact legacy source documents;
- show allowed actions without making the read surface itself authoritative.

Search and filtering belong in the first useful read-only product rather than requiring a later
editor. They may start narrowly and expand as the structured schema establishes useful fields.

### Stage 2: structured editing and human actions

- create a bare task with a title-only form;
- offer structured forms only inside lifecycle-authorized commands;
- use text or Markdown editor components only for fields whose approved type is long prose;
- expose Marco's existing private interventions with their exact preconditions;
- show backup health and cutover/import quarantine status;
- later append cook logs through a separately designed command.

### Stage 3: cooking planner

A later board may organize dishes into concepts such as Cook Now, Cook Soon, Cook Later, and
Unscheduled, with ordering or priority where useful. These names and their storage are
illustrative, not an approved enum or table design. Design work must decide whether planning is a
single horizon, an ordered queue, dates, independent flags, or some combination, and how it
interacts with completion, locations, workflow ownership, and multiple UI sessions.

Planning buckets are not workflow sections or canonical destinations. Dragging within or between
them may become a convenient way to invoke a named, revision-checked planning command. Dragging
must not directly patch rows, move a task through Research or Verification, change a canonical
destination, invalidate signoff, or infer that any section-like UI column is lifecycle authority.
The service returns whether the exact planning action is allowed and records whatever audit or
transition evidence the approved planner design requires.

Before implementing this stage, separately approve the planning concepts, ordering semantics,
command contract, concurrency behavior, history requirements, and which actions are reversible. Do
not add generic task-section movement merely to support a board interaction.

## Cross-stage invariants

Across these stages, the frontend preserves the distinction between task organization, workflow
state, canonical destination, and completion. It derives mutation controls from the service's exact
authoritative snapshot.

The reusable frontend shell—list, search, read, history, status, and narrow action controls—may be
built while Asana remains authoritative. It calls `dish-service`, not either store directly. A
shadow-backed read view must expose its source snapshot and freshness, and it never authorizes a
mutation; the service rechecks the live Asana task until cutover.

Defer the complete dish editor until the structured command schema is stable. The target editor is
a structured form over typed fields and collections. Established text or Markdown components may
improve long prose fields such as instructions, but editor-specific state and a whole-document
Markdown blob do not become canonical dish data.

There is no generic canonical-content save command. Revision and exact-version checks protect
concurrency, but they do not confer authority to create a new current structured version. Content
legality is state-based:

- a bare task is created title-only with empty body; a narrow command may change its title while it
  remains bare;
- a Planning brief is authored or changed only through the Planning workflow;
- a governed canonical task is authored or changed only through the applicable Research, Change,
  correction, or explicitly designed Marco lifecycle operation;
- a signed or destination task has no ordinary save action; changing it starts Change or another
  named lifecycle operation that invalidates or supersedes evidence explicitly;
- a completed task must be reopened or cloned through a named command before content changes.

The service derives these actions from the authoritative task and workflow snapshot. Merely having
no active operation is not sufficient: an inactive task may still be signed, submitted,
destination-placed, or completed. An edit control may therefore be read-only or absent even though
the task has no current owner.

When an authorized lifecycle command accepts edited fields, the browser loads the task identifier,
exact structured version, monotonic revision, action identity, and any operation/run authority that
command requires. It submits a complete versioned JSON candidate with those expectations and a
fresh request UUID. `dish-service` reasserts lifecycle legality, validates the structured graph,
rejects stale state without overwriting either version, appends the new immutable version, advances
the task pointer, and records the required lineage, governed audit, and replay result in one
transaction. The UI then renders the committed canonical result or presents the newer current
version for explicit reconciliation. Silent last-write-wins and editor-level force-save behavior
are prohibited.

The frontend must not impersonate an agent or invent run lineage. Agent workflow actions remain on
the authenticated agent surfaces. If a future UI hosts an authenticated agent session, it may render
only the actions returned for that exact principal and run.

Before cutover, inventory the human actions currently performed in Asana. At minimum, define how a
bare task is created, how completed cooking history is searched, and how a cooked task is marked
complete. Any required replacement is a narrow command with explicit preconditions and audit—not a
generic row or content editor. Structured content is accepted only by the lifecycle command
authorized for the current state. These commands, available through CLI/admin if necessary, are the
frontend-independent prerequisite for DB authority.

The browser never sends SQL, chooses arbitrary state transitions, patches task rows, or derives
legal actions. State-changing UI controls call the same command applications as CLI/admin routes
with fresh request UUIDs and render the canonical result envelope.

The frontend is served only on the private listener or through a same-origin private companion.
Marco's admin bearer credential must not be stored in frontend source, URLs, logs, or browser
persistent storage. The chosen UI architecture must preserve the existing private-versus-Action
credential boundary and must not make admin routes reachable from the Funnel listener.

## Relationship to the migration rehearsal

This was previously also referenced from the migration/cutover plan as an optional Phase 3:

> The list, search, read, history, status, and narrow action shell may run before cutover through
> `dish-service`. Authoritative views and mutation preconditions still come from Asana. Candidate DB
> views may be exposed only with source/freshness labels and may not authorize actions.
>
> This phase is useful but optional. A polished frontend is not a cutover gate if equivalent narrow
> CLI/admin commands cover every required human mutation. Defer the full structured editor until the
> structured command schema is stable.

Rehearsal could optionally exercise the private frontend (or equivalent CLI/admin commands) against
the imported copy, but this is not a Stage A cutover requirement — see
[`database-backend.md`](database-backend.md) and
[`database-backend-migration.md`](database-backend-migration.md) for the current, authoritative
cutover gates, which do not depend on this frontend existing.

## Provenance

This content was drafted as part of the original single-file `database-backend-design.md` (commit
`2b7e354` onward) and removed in commit `bc24b37` ("update db doc", 2026-07-31) without a recorded
rationale, before the remaining file was later split into `database-backend.md`,
`database-backend-imp.md`, and `database-backend-migration.md` (commit `d6acabb`). It is restored
here verbatim from the pre-removal version (`git show 63736b2:dish/docs/database-backend-design.md`)
so the design isn't stranded in git history.
