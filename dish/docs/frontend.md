# Dish private frontend design

**Status: Stage 1 product design approved. Implementation is authorized only stage by stage; real authentication and canonical-data integration remain blocked until the evidence gates in [`frontend-imp.md`](frontend-imp.md) pass.**

This document defines the approved private frontend product over Dish's PostgreSQL-backed service.
It describes product behavior, user-visible information, authority boundaries, and acceptance
outcomes. Every requirement introduced because this frontend needs it belongs to the frontend work,
even when the implementation lands in the service, backend, PostgreSQL layer, deployment, or tests.
The corresponding mechanics belong in [`frontend-imp.md`](frontend-imp.md).

Read [`architecture.md`](architecture.md), [`runtime-contract.md`](runtime-contract.md),
[`database-backend.md`](database-backend.md),
[`database-backend-imp.md`](database-backend-imp.md), and
[`database-backend-migration.md`](database-backend-migration.md) as governing authority contracts.
The frontend must preserve their shared authority invariants. These frontend documents are the
additive governing contract for every frontend-specific product, access, service, and PostgreSQL
requirement, including requirements not present in the older pre-frontend runtime surface. They do
not reopen, amend, delay, or expand the database work. Where this frontend is narrower, the existing
backend obligation remains.

## 1. Purpose

Dish needs a private human-facing view of the canonical PostgreSQL task state without making the
browser a second authority system.

Stage 1 provides a fast, readable, desktop board for the sole human user. It lets that user find,
open, and understand every non-retired, incomplete task in the active logical section registry.
It does not allow the user to alter canonical content, workflow, completion, placement, projection,
or any other governed task-authority state.

## 2. Product stages

### 2.1 Stage 1: private read-only board

Stage 1 provides:

- an Asana-style board whose columns are the authoritative logical sections;
- compact incomplete-task cards in deterministic automatic title order;
- current task detail in a fixed-width side panel;
- factual workflow and attention information;
- plain-English, non-authorizing guidance about what needs to happen next;
- automatic background refresh while preserving the last successful usable view;
- a consistent top-of-screen warning and error system;
- a simple shared-password login with a seven-day session;
- a desktop-focused experience.

Stage 1 contains no global search page, completed-task view, history browser, generic save,
drag-and-drop mutation, workflow command, administrative intervention, cutover control, or write to
canonical task, workflow, placement, completion, projection, or other governed authority state.
Frontend-owned session and security-audit storage is permitted only for the private access path and
does not become task or workflow authority.

### 2.2 Stage 2: structured editing and human actions — future work

Stage 2 is not approved by this document. Any future mutation design must preserve these invariants:

- no generic canonical-content save;
- no arbitrary database-row patching;
- no browser-owned workflow-legality engine;
- every mutation is a named backend command with an exact principal, replay identity, current-view
  and concurrency fences, and governed audit behavior;
- the frontend never impersonates an agent or invents run lineage.

Editor shape, payload design, reconciliation experience, and the exact set of human commands require
a separate reviewed design.

### 2.3 Stage 3: cooking planner — future work

A future planner may organize dishes into planning concepts such as Cook Now, Cook Soon, Cook Later,
and Unscheduled. Those concepts are not workflow sections, canonical destinations, or completion
states. A visual gesture may invoke only an approved planning command and must never directly patch
rows or move a task through governed workflow.

## 3. Stage 1 experience

### 3.1 Board

The landing view is one horizontally scrollable board. Columns do not wrap onto multiple rows.

The board behaves as follows:

- every active logical section appears as a column, including empty sections;
- columns use the authoritative section order;
- when section labels are ambiguous across projects, the column header also shows the human-readable
  project label; every displayed project-plus-section path must be distinct under the checked-in
  display-label normalization contract, and the board fails visibly rather than showing ambiguous
  columns when configuration cannot provide a unique normalized path;
- completed and retired tasks are always hidden, with no control to reveal them;
- every task remains in its authoritative logical section;
- tasks within each section are ordered by normalized title ascending, with Dish task UUID as the
  deterministic tie-breaker;
- Stage 1 stores no manual card position or user-defined task order;
- cards are compact so that many tasks remain visible at once;
- Stage 1 has no drag-and-drop, personal column order, personal card order, alternate list view,
  global title search, or cross-section filtering.

The first view loads every section and the first bounded page of non-retired, incomplete tasks for
each section. An active registry with no sections is a valid empty-board state, not an error.
A section with additional tasks shows an explicit **Load more** control. Stage 1 does not use
infinite scrolling.

### 3.2 Task cards

Each card shows:

- the task title;
- one compact factual status line showing the current operation and phase when present, or
  **No active operation** when no operation is active;
- small attention indicators only for the approved Stage 1 categories: lease attention, Verification
  attention, active hold, required recovery, active abandonment, active succession, or abnormal
  projection.

Attention facts do not move the task into a synthetic column. The card remains in its authoritative
section. Every active attention category represented by the currently loaded board pages or open task
detail also participates in the common banner treatment, with repeated instances grouped rather than
shown as separate banners. While fresh detail is successfully open, its attention state supersedes the
selected card's stale attention state for banner grouping until the panel closes or detail becomes
unavailable.

Cards do not show technical identifiers, raw policy output, canonical `allowed_actions`, or controls
that imply mutation authority. New attention categories cannot be inferred or invented by the browser;
they require an explicit frontend-contract update.

Selecting a card opens the task in the side panel. Stage 1 has no dedicated **Open in new tab**
control and no separate full-page task screen. The selected task is represented in the URL by a non-raw, non-sensitive browser-facing identity so
that a reload or revisited deep link restores the same board-plus-panel view without exposing a
canonical database or external-system identifier.

### 3.3 Task side panel

The task side panel:

- has a fixed width;
- keeps the board visible;
- uses one vertically scrolling page rather than tabs;
- closes through its close control, the Escape key, or a click outside the panel;
- remains open through background refresh while the selected task remains eligible for the board;
- shows human-readable information rather than technical identifiers or diagnostics.

The panel shows the task's current canonical content and current factual state only. Stage 1 has no
history timeline or historical-content browser.

The panel must show:

- canonical title and body in the approved safe rendered form;
- logical project and section labels;
- current factual workflow status, including operation and phase when present;
- named attention facts approved for human disclosure when present, including lease, Verification,
  hold, recovery, abandonment, or succession information;
- a plain-English explanation of what needs to happen next.

The panel also shows canonical destination when present and abnormal downstream projection
information whenever an abnormal projection state is present. The panel never shows partial or
executable task content. If an otherwise supported canonical body is rejected by the approved renderer
or sanitizer, the panel shows an inert plain-text fallback with the common warning treatment; if no
safe bounded presentation can be produced, the board remains usable and the common error treatment
applies. A completed or retired task is not a Stage 1 detail state: the panel closes and the common
banner explains that the task left the board.

The next-step explanation is descriptive only. It is not a command, bearer capability, button, or
substitute for principal-aware legal-action computation. The frontend does not receive or present
canonical `allowed_actions`.

Healthy projection state stays out of the way. Projection delay, failure, drift, unknown state, or
unavailability appears only as an abnormal condition through the common warning/error treatment.

### 3.4 Refresh and continuity

The board and an open task panel refresh automatically in the background.

Refresh behavior is fixed:

- the last successful board remains visible while a refresh is in progress;
- a temporary refresh or service failure does not replace a usable board with a full-screen error;
- if the initial board load fails before any usable board exists, the board shell remains visible with
  the common banner treatment and an explicit retry path;
- an open task panel refreshes in place when possible;
- opening a task always retrieves fresh detail rather than trusting the card as current authority;
- a task that moves appears only in its new authoritative section after refresh;
- a task that becomes completed or retired disappears from the board;
- if the selected task becomes completed, retired, or otherwise ineligible for the Stage 1 board,
  the panel closes and a banner explains why;
- when changed pagination makes additional loaded pages unsafe to retain, the affected column resets
  to its first page and the user may use **Load more** again;
- recovered conditions clear their warning automatically;
- Asana is never used as a fallback when canonical service data is unavailable.

### 3.5 Warnings and errors

Every warning or error uses a slim, full-width banner area at the top of the screen.

- distinct simultaneous conditions stack;
- repeated instances of the same underlying condition across the currently accepted board pages and
  open task detail are grouped into one banner with a truthful affected-task count;
- messages use plain language;
- the board and last successful data remain visible whenever continued viewing is safe;
- an ongoing condition remains visible until it resolves;
- only informational, no-longer-active notices may be dismissed;
- a banner may link or scroll to relevant detail without becoming a mutation control.

Task-card indicators remain available to locate affected tasks, but warning and error communication
uses the common banner system rather than unrelated modal dialogs or replacement screens. Login and
session errors use the same banner treatment while remaining programmatically associated with the
relevant form or control.

### 3.6 Login and session experience

Stage 1 uses one shared password for its sole human user.

- successful login creates a session that remains valid across browser restarts for a fixed seven
  days from login;
- activity does not extend the seven-day deadline;
- logout ends only the current browser session;
- Stage 1 has no separate **log out all sessions** control;
- changing the shared password invalidates all existing sessions;
- destructive restore or recovery cannot make an expired or revoked frontend session valid again;
- the browser never handles or displays backend agent, admin, or Action bearer credentials.

The login experience is private and same-origin. Stage 1 does not require the user to paste a backend
bearer token. An unauthenticated task deep link returns to the same board-plus-panel view after a
successful login. Logout or session expiry clears protected task content before returning to login.

### 3.7 Device profile

Stage 1 is desktop-focused. Responsive tablet and phone layouts are not part of the first release.
The implementation defines and tests a minimum supported desktop viewport. Smaller viewports may
remain usable where practical but are not an acceptance requirement.

## 4. Information and authority design

### 4.1 Canonical authority

All task content, logical placement, board eligibility, workflow facts, and next-step guidance shown
by the frontend come from Dish's canonical service over PostgreSQL authority.

The browser:

- does not read PostgreSQL directly;
- does not call Asana directly;
- does not infer workflow legality;
- does not compute canonical next actions;
- does not treat cached or displayed data as mutation authority;
- does not become a fallback authority when the service is unavailable.

### 4.2 Factual summaries versus authority

Board cards are compact, staleable factual summaries. They are sufficient for discovery but are not
current-view or mutation authority. Opening a task retrieves a fresh canonical detail view.

Completion, destination, operation, phase, Verification, lease, hold, recovery, abandonment,
succession, and projection remain distinct facts. The frontend does not collapse them into one
generic authoritative task status or `blocked` value.

### 4.3 Next-step guidance

Plain-English next-step guidance is produced from backend-owned workflow facts. It explains what
needs to happen without claiming that the browser or current session may perform it.

The guidance:

- is read-only;
- is not named `allowed_actions`;
- does not impersonate an agent or run;
- does not expose raw commands or private administrative continuations;
- cannot be used by the browser to decide legality.

### 4.4 Aliases and projection

Imported Asana identifiers and downstream projection evidence are separate concepts.

- imported identifiers are historical external aliases with provenance;
- projection information describes the downstream Asana representation and reconciliation evidence;
- an imported alias never proves a current projection mapping or healthy projection state;
- projection delay or drift never changes canonical workflow legality.

Healthy projection state is hidden. Abnormal projection state is disclosed only when it helps the
user understand a warning or the current task.

### 4.5 Disclosure

The frontend displays only information required by the approved board and side panel. It does not
show raw request, replay, run, current-view, audit, execution, reconciliation, infrastructure, or
database details.

Agent and actor facts may be converted into approved human-readable labels when needed to understand
the task, but raw technical identities are not part of Stage 1 presentation.

## 5. Scope exclusions

Stage 1 does not include:

- completed-task browsing;
- global task search or cross-section filtering;
- task history or historical document versions;
- task creation;
- content editing;
- drag-and-drop movement;
- workflow, recovery, completion, Cooked, or Archive controls;
- administrative intervention;
- legal-action filtering or sorting;
- technical diagnostics;
- direct database or Asana access;
- tablet or phone acceptance requirements.

Omitting a backend capability from Stage 1 does not authorize its removal, deferral, narrowing, or
replacement.

## 6. Stage 1 acceptance

Stage 1 is complete only when the product demonstrates that:

- the user can find, open, and understand every non-retired, incomplete task placed in the active
  logical section registry without relying on Asana;
- every active logical section appears in authoritative order, including empty sections;
- completed or retired tasks never appear on the board or in board pagination;
- tasks remain in their authoritative section and follow normalized-title order with Dish task UUID
  as the deterministic tie-breaker;
- cards remain compact while communicating title, the operation/phase status line, and only the
  approved attention categories;
- **Load more** extends only the chosen section;
- opening a card obtains fresh task detail;
- the side panel shows the required canonical content, factual state, and non-authorizing next-step
  guidance, never shows partial or executable task content, uses the inert plain-text fallback with a
  warning for an otherwise supported body rejected by the renderer or sanitizer, and preserves the
  usable board with the common error treatment when no safe bounded presentation can be produced;
- canonical `allowed_actions` are never exposed through the Stage 1 presentation;
- automatic refresh moves, removes, or updates tasks without presenting duplicate authoritative
  placement;
- temporary failure preserves the last successful usable board, while an initial-load failure keeps
  the board shell visible with the common banner and retry path;
- distinct warnings stack, every active loaded attention category is represented through the common
  banner treatment, and repeated instances across accepted board pages and open detail are grouped with
  a truthful count;
- healthy projection state is suppressed and abnormal projection state is clearly non-authoritative;
- a valid login survives browser and ordinary service restart until its fixed seven-day expiry;
- logout ends only the current session, password rotation invalidates all sessions, and destructive
  restore or recovery cannot make an expired or revoked session valid again;
- deep-linked task URLs restore the selected task panel;
- the desktop minimum viewport supports the horizontal board and fixed-width panel, and the board,
  cards, panel, close control, **Load more**, login, logout, and banners remain keyboard operable with
  visible focus;
- no Stage 1 interaction mutates canonical state or creates a second workflow authority.

Technical evidence and test requirements for these outcomes are defined in
[`frontend-imp.md`](frontend-imp.md).

### 6.1 Staged delivery and design review

Stage 1 is delivered through reviewable increments rather than as one final board revealed only at
completion. The implementation plan in [`frontend-imp.md`](frontend-imp.md) must provide early
runnable deliverables that let the human user review layout, density, labels, navigation, warning
treatment, and task-detail presentation before the complete backend integration is finished.

Each user-visible delivery stage ends with a deliberate design-review gate. Feedback may confirm the
current design or require a targeted update to these frontend contracts before the next affected stage
continues. Approval of an intermediate deliverable does not waive the remaining Stage 1 requirements
or authorize unreviewed product behavior. Fixture-backed prototypes are review tools only and never
become canonical authority.

The staged plan does not make a document-wide claim that every integration dependency is already verified. Delivery Stages 0 and 1 may begin because they establish structure and obtain visual feedback without claiming real task authority. Before Delivery Stage 2, the complete contract and authentication/runtime dependencies must pass the independent readiness review in `frontend-imp.md`. Before Delivery Stage 3, every real board, detail, projection, and attention field must have an accepted code-grounded source and predicate map. An implementation agent may not infer missing semantics from field names or continue past either gate on the basis that the remaining work is probably straightforward.

Stage 1 is not complete until the final integrated product passes its automated acceptance suite and a
committed, repeatable Playwright browser-acceptance suite (authored by a capable local agent, such as
Claude, with its first full run performed against a production-shaped local environment) as defined in
[`frontend-imp.md`](frontend-imp.md). That suite is part of the Stage 1 delivery and remains in the
repository as a standing regression gate afterward, not a one-time walkthrough or a future feature.

## 7. Cross-stage invariants

Across all frontend stages, task organization, workflow state, canonical destination, completion,
planning, and projection remain separate concepts. The frontend calls Dish's service rather than
PostgreSQL or Asana directly and contains no second workflow-policy implementation.

Future state-changing controls must invoke named command applications with exact principal, replay,
current-view, and concurrency requirements. The service reasserts legality and returns the canonical
result. The browser never chooses arbitrary transitions, sends SQL, patches task rows, or forces
last-write-wins behavior.

Completion becomes true only through the governed Cooked or Archived transition. There is no generic
mark-completed frontend action.

## 8. Relationship to backend authority

The frontend is a separate product surface over the PostgreSQL backend. It is not a migration stage,
cutover authority, production fallback, or alternative workflow engine. Frontend defects cannot
authorize return to Asana or alter database authority.

All work required specifically to deliver this frontend remains frontend-owned, regardless of which
code layer implements it. That includes presentation APIs, application/query services, PostgreSQL
queries, read models, indexes, pagination, ordering, frontend-specific storage, authentication,
deployment support, and acceptance tests. These requirements are specified in
[`frontend-imp.md`](frontend-imp.md). That document is the additive contract for the frontend access
path while the older runtime contracts continue to govern pre-existing callers and shared admission,
authority, and shutdown invariants. The database design and implementation documents are not reopened
or amended for frontend-driven work.

## 9. Provenance

The original direction was drafted inside the former single-file database design and later restored
as a separate frontend proposal. This design records the approved Stage 1 product and authority
behavior. Implementation mechanics are isolated in [`frontend-imp.md`](frontend-imp.md).

## 10. Needs spec

Some product questions are not yet assigned to an approved or future stage above because no design
exists for them at all. Listing a question here does not approve, scope, or schedule it; it exists so
that a future contract update starts from a named question rather than silent invention during
implementation of some other stage.

- **Admin action exposure.** Whether, and under what principal and authority model, any `dish-admin`
  action (for example `recover-lease`, `abandon-operation`, `reconcile-abandonment`,
  `repair-destination`, or hold resolution) is ever invoked from the frontend, rather than remaining a
  Marco-only terminal tool. Today `dish-admin` production use is Marco-only and the frontend has no
  administrative-intervention surface at all. No scope, principal model, mutation shape, or stage
  assignment is decided by this entry; a dedicated design must resolve it before any related
  implementation begins.
