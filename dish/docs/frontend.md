# Dish private frontend

**Status: draft — under review, not ready for implementation.** Nothing in this file authorizes
frontend implementation yet. The proposed Stage 1 timing is after database-backend Stage 4 has
produced a usable PostgreSQL-backed `dish-service` in a non-production environment, with frontend
work then proceeding in parallel with backend Stages 5 and 6. Stage 2 mutations and the Stage 3
cooking planner remain unapproved future work. This is a separate product track, not a Stage A
deliverable. The frontend is not a database-cutover prerequisite or production-authority surface.

Read [`database-backend.md`](database-backend.md),
[`database-backend-imp.md`](database-backend-imp.md), and
[`database-backend-migration.md`](database-backend-migration.md) as the governing authority,
implementation, and cutover contracts. The frontend never weakens or replaces those contracts.

## Delivery stages

### Stage 1: reading and discovery — proposed

If this draft is approved, Stage 1 begins only when backend Stage 4 is implemented and the frontend
can connect to a real PostgreSQL-backed service. It does not begin against speculative mocks as the primary development
surface. Mocks and fixtures remain useful for component tests, but integration starts from the real
service contract.

The proposed first useful product includes:

- a task list with bounded pagination, search, and filters by title, logical location, completion,
  destination, and active-operation state;
- task detail showing the exact current authoritative document and rendered view;
- authoritative location, completion, operation, lease, hold, Verification, recovery, and allowed
  action state;
- content-version, workflow, Verification, audit, and recovery history;
- the canonical title/body document and its rendered form, without introducing structured-dish
  authority into Stage A;
- projection freshness and drift status shown separately from authoritative PostgreSQL state when
  the Stage 5 projection contract is available;
- loading, empty, stale, unavailable, and conflict states that do not invent authority.

The proposed first release is read-only. Displaying an allowed action does not authorize the browser to invoke
it. Stage 1 contains no generic save, drag-and-drop mutation, workflow command, or admin intervention.

### Stage 2: structured editing and human actions — future work

- create a bare task with a title-only form;
- offer structured forms only inside lifecycle-authorized commands;
- use text or Markdown editor components only for fields whose approved type is long prose;
- expose Marco's existing private interventions with their exact preconditions;
- show backup health and cutover/import quarantine status;
- later append cook logs through a separately designed command.

Before implementation, each mutation must receive an exact command contract, principal, request and
replay identity, current-view and fence requirements, error/result behavior, and concurrency tests.
A visual gesture cannot conceal a governed transition.

### Stage 3: cooking planner — future work

A later board may organize dishes into concepts such as Cook Now, Cook Soon, Cook Later, and
Unscheduled, with ordering or priority where useful. These names and their storage are illustrative,
not an approved enum or table design. Design work must decide whether planning is a single horizon,
an ordered queue, dates, independent flags, or some combination, and how it interacts with
completion, locations, workflow ownership, and multiple UI sessions.

Planning buckets are not workflow sections or canonical destinations. Dragging within or between
them may become a convenient way to invoke a named, revision-checked planning command. Dragging
must not directly patch rows, move a task through Research or Verification, change a canonical
destination, invalidate signoff, or infer that any section-like UI column is lifecycle authority.
The service returns whether the exact planning action is allowed and records whatever audit or
transition evidence the approved planner design requires.

Before implementing this stage, separately approve the planning concepts, ordering semantics,
command contract, concurrency behavior, history requirements, and which actions are reversible. Do
not add generic task-section movement merely to support a board interaction.

## Proposed Stage 1 implementation contract

### Backend entry condition

The proposed Stage 1 entry condition is satisfied only when database-backend Stage 4 has delivered:

- PostgreSQL-backed task identity, document, location, completion, workflow, and current-view reads;
- stable bounded list, detail, and history query semantics;
- authority-generation and current-view tokens;
- a usable private service in a non-production environment;
- generated OpenAPI that matches the implemented FastAPI routes.

Backend Stages 5 and 6 may still be running import, shadow, projection, fault testing, rehearsal, and
rollout work. Projection freshness may initially be absent or explicitly `not_configured`; the UI must
not infer projection state or substitute Asana data. Frontend availability and polish do not gate
those stages.

### API framework and trust boundary

The proposed frontend-facing API uses **FastAPI** on the existing private `dish-service` listener.
FastAPI is the HTTP and OpenAPI framework; it is not a new domain, authority layer, or replacement
service. The route layer reuses the existing scoped-bearer authentication and authorization model
owned by `dish_service.auth`.

The FastAPI route layer must:

- call application/query services rather than opening ad hoc SQL sessions in route handlers;
- return typed response models whose OpenAPI schema is generated and checked into or validated by
  the repository's existing OpenAPI synchronization tests;
- expose only private frontend routes on the private listener;
- keep Action/Funnel routes and credentials separate;
- never call Asana to determine canonical workflow, legality, or task content after cutover;
- include authority generation, current-view identity, and projection freshness where required by
  the response contract.

The frontend uses a generated or schema-checked typed client based on the FastAPI OpenAPI document.
The frontend framework and component library remain implementation choices.

The browser must never receive Marco's admin bearer credential, an agent CLI token, or the Action
token. Proposed Stage 1 uses a dedicated **frontend-read bearer token** implemented by the existing
scoped-token mechanism. The proposed credential is environment-specific, follows the existing named
profile pattern without generic fallback, and authorizes only the frontend GET routes defined by an
approved version of this design. It is invalid on CLI/admin and Action routes and is authenticated by
the shared HTTP auth layer before handler execution or protected-body parsing, in
the same way as the existing protected surfaces. It must not be embedded in source, URLs, logs,
local storage, or session storage. The initial private UI may accept it at browser startup and retain
it only in process memory. Frontend routes must not become reachable from the Funnel listener.

A later mutation stage must not silently widen the frontend-read token. Its principal and route
scopes require separate review together with the exact mutation contracts.

### Proposed read operations

Exact URL spelling is an implementation detail, but the FastAPI contract must provide these logical
operations.

#### Task list

A bounded, paginated task query returns at least:

- Dish UUID and display title;
- current logical project and section/location;
- completion;
- canonical destination where present;
- current operation and phase summary;
- active lease, hold, or blocking-condition summary;
- authoritative task revision and authority generation;
- projection freshness as non-authoritative metadata.

The query supports title search and filters for logical location, completion, destination, and
active-operation state. Pagination uses a stable server-defined ordering and opaque cursor. The list
is a bounded relational read over authoritative factual state; it does not calculate, materialize,
sort, or filter by legal actions. A page must not depend on per-task Asana reads or per-task
application-service policy calls.

#### Task detail

A task-detail query returns:

- canonical title/body document and its rendered form;
- immutable version identity, active revision, authority generation, and current-view token;
- logical membership, placement, completion, destination, workflow, lease, hold, Verification, and
  recovery state;
- allowed actions for display only;
- Asana mapping and projection freshness separately from canonical state.

#### Task history

One or more bounded history queries return authoritative occurrences for:

- content versions and activations;
- operations, steps, actor facts, and leases;
- Verification cycles, inspection, correction, approval, and signoff lineage;
- holds, recovery, abandonment, and succession;
- governed audit and relevant projection/reconciliation history.

History ordering is defined by authoritative occurrence sequence or durable database ordering, not a
client-side merge of loosely comparable timestamps. Each history response states whether more data
is available.

### Consistency and error behavior

The frontend treats every response as a view of a particular authority generation and current-view
identity.

- If task state changes after a list page is loaded, opening the task fetches current detail rather
  than trusting the list row as authority.
- A stale current-view token, revision, or authority generation produces a refresh/reload path. The
  browser never silently overwrites newer state.
- PostgreSQL/service unavailability is shown as authoritative data unavailable; stale Asana state is
  not substituted.
- Projection delay or failure is shown as downstream freshness/drift, not as uncertain canonical
  workflow state.
- Partial history loading is explicit and cannot be mistaken for complete history.
- If an allowed action changes before a later mutation stage invokes it, the service rejection and
  new canonical snapshot win; the UI does not locally force the action.

### Proposed Stage 1 acceptance

If Stage 1 is approved, it is complete only when tests prove:

- every displayed workflow, legality, location, completion, and action fact comes from
  `dish-service` and PostgreSQL authority;
- direct Asana changes cannot alter the authoritative state displayed by the frontend;
- list fields agree with authoritative PostgreSQL facts, while task-detail allowed actions agree with
  the backend's existing per-task current-view and workflow-policy computation;
- search and filters use bounded server-side queries and include governed tasks with no current or
  historical operation row;
- pagination under the declared ordering does not duplicate or omit records in stable test data;
- stale revisions, current-view tokens, and authority generations produce an explicit refresh path;
- projection freshness is visually and structurally separate from authoritative state, and an absent
  Stage 5 projection reports an explicit not-configured/unavailable state rather than inferred data;
- credentials do not enter frontend source, URLs, logs, local storage, or session storage;
- the environment-specific frontend-read token is accepted only on its private read routes and is
  rejected on CLI/admin and Funnel/Action routes;
- private FastAPI routes are unavailable from the Funnel/Action listener;
- generated OpenAPI and the typed frontend client remain synchronized with implemented routes;
- the sole human user can find, open, and understand every current task without relying on Asana.

## Cross-stage invariants

Across all stages, the frontend preserves the distinction between task organization, workflow state,
canonical destination, and completion. It derives displayed action availability from the service's
exact authoritative snapshot.

The frontend calls `dish-service`, not PostgreSQL or Asana directly. It does not contain a second
workflow-policy implementation. Stage 1 list endpoints expose stored or relationally derived factual
summaries only; they do not expose or filter by legal-action results. Task-detail allowed actions are
computed by the existing authoritative workflow-policy layer from that task's exact current snapshot.
No frontend query model, cache, or materialized view becomes a second source of workflow legality.

Defer the complete dish editor until the structured command schema is stable. The target editor is a
structured form over typed fields and collections. Established text or Markdown components may
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
command requires. It submits a complete versioned JSON candidate with those expectations and a fresh
request UUID. `dish-service` reasserts lifecycle legality, validates the structured graph, rejects
stale state without overwriting either version, appends the new immutable version, advances the task
pointer, and records the required lineage, governed audit, and replay result in one transaction. The
UI then renders the committed canonical result or presents the newer current version for explicit
reconciliation. Silent last-write-wins and editor-level force-save behavior are prohibited.

The frontend must not impersonate an agent or invent run lineage. Agent workflow actions remain on
the authenticated agent surfaces. If a future UI hosts an authenticated agent session, it may render
only the actions returned for that exact principal and run.

Before any mutation stage, inventory the human actions currently performed in Asana. Any required
replacement is a narrow command with explicit preconditions and audit—not a generic row or content
editor. Structured content is accepted only by the lifecycle command authorized for the current
state. These commands must remain frontend-independent and available through the service and narrow
CLI/admin surfaces where required.

The browser never sends SQL, chooses arbitrary state transitions, patches task rows, or derives legal
actions. State-changing UI controls call the same command applications as CLI/admin routes with
fresh request UUIDs and render the canonical result envelope.

## Relationship to backend rollout

If approved, Frontend Stage 1 starts after backend Stage 4 is implemented and may run throughout
backend Stages 5 and 6. It is a separate parallel product track rather than an added Stage A gate. It
can help exercise real PostgreSQL-backed reads during import, shadow, projection, and rehearsal, but
it remains observational and non-gating.

The final production cutover does not depend on the frontend. Equivalent CLI/admin and service
surfaces must remain sufficient for required operations. Frontend defects cannot authorize fallback
to Asana or alter the database authority transition.

## Provenance

The original direction was drafted as part of the single-file `database-backend-design.md` (commit
`2b7e354` onward) and removed in commit `bc24b37` ("update db doc", 2026-07-31) without a recorded
rationale, before the remaining file was split into `database-backend.md`,
`database-backend-imp.md`, and `database-backend-migration.md` (commit `d6acabb`). It was restored
from the pre-removal version (`git show 63736b2:dish/docs/database-backend-design.md`) and has now
been narrowed into a proposed Stage 1 read-only design while retaining Stage 2 and Stage 3 as future
work. It remains a draft under review and does not authorize implementation.
