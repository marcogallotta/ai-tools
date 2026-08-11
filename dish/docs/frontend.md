# Dish private frontend product contract

Status: the read-only frontend is implemented but disabled pending the
exact-build acceptance recorded in
[`frontend-activation.md`](frontend-activation.md).

This document owns durable user-visible behavior and scope. Architecture owns
trust, authority, and runtime boundaries; the frontend OpenAPI document owns
wire shapes; tests own executable acceptance; and
[`frontend-deployment-runbook.md`](frontend-deployment-runbook.md) owns
operations.

## Purpose and scope

The frontend gives the sole human user a fast desktop view of Dish's canonical
task state without making the browser a second authority. The current product is
a private, read-only board with task detail, background refresh, warnings, and
shared-password authentication.

It does not provide search, completed-task browsing, history, editing, task
creation, drag-and-drop, workflow or recovery commands, administrative
intervention, cutover controls, or direct PostgreSQL or Asana access.
Frontend-owned authentication state is not task or workflow authority.

## Board

- Show every active logical section in authoritative order, including empty
  sections.
- Show every ordinary or isolated, incomplete, non-retired task with a current
  eligible placement in the active registry. Isolated tasks remain visible and
  are marked **ISOLATED**.
- Keep each task in its authoritative section. Order cards by normalized title,
  with Dish task UUID as the deterministic tie-breaker.
- Load one bounded first page per section in the initial board response. A
  section with more tasks has an explicit **Load more** control; there is no
  infinite scroll.
- Keep columns on one horizontally scrollable row. There is no manual card or
  column ordering, alternate list view, cross-section filter, or drag-and-drop.
- Fail visibly when project/section labels cannot form distinct display paths
  rather than presenting ambiguous columns.

Cards show the title, an operation/phase line when meaningful, and only
registered attention categories: isolation, lease, Verification, hold, recovery,
abandonment, succession, and abnormal projection. Attention does not create
synthetic columns. The browser does not infer new categories or display raw
identifiers, diagnostics, policy output, or `allowed_actions`.

## Task detail and routes

Selecting a card fetches fresh detail and opens a fixed-width, vertically
scrolling side panel while the board remains visible. The panel closes through
its control, Escape, or a click outside. It stays open across refresh while the
task remains board-eligible.

The route is `/dishes/<stored-dish-uuid>/<decorative-title-slug>`. The Dish UUID
is the identity; the slug is presentation only. Reload and browser history
restore the same board-plus-panel state. The UUID is not a credential.

Detail shows current canonical title and safely rendered body, project and
section, factual workflow state, registered disclosures, canonical destination
when present, abnormal projection information, and plain-language next-step
guidance. Supporting Process Record material is collapsed by default.

Guidance is descriptive, not an action or authorization. The panel does not
expose raw commands, legal actions, aliases, request/replay/run/current-view
identities, audit or reconciliation evidence, infrastructure details, or
database fields.

If supported content cannot pass the renderer or sanitizer, show an inert
plain-text fallback with a warning. If no safe bounded presentation is possible,
keep the board usable and show the common error treatment. If the task becomes
completed, retired, or otherwise ineligible, close the panel and explain that it
left the board.

## Refresh, warnings, and continuity

- Refresh the board and open detail automatically while the page is active.
- Preserve the last successful usable view during refresh and temporary
  domain-read failures when session validity is still established.
- On initial failure, retain the board shell, show the common banner, and offer
  retry.
- Opening detail always reads current data; cards are staleable summaries.
- Reconcile moved, completed, retired, and changed tasks without showing
  duplicate authoritative placement. Reset only a pagination chain that is no
  longer safe.
- Never fall back to Asana when canonical service data is unavailable.

Warnings and errors use a slim full-width banner region. Distinct conditions
stack; repeated instances are grouped with a truthful affected-task count.
Active conditions remain until resolved, while inactive informational notices
may be dismissed. The last safe view remains visible whenever continued display
is safe.

Healthy projection is omitted. Delay, failure, drift, uncertainty, or
unavailability is shown only as non-authoritative abnormal projection
information. Projection never changes workflow legality.

## Login and session experience

The frontend uses one shared password and a server-managed browser session
scoped only to frontend reads and its own lifecycle.

- A successful login has a fixed seven-day lifetime that survives browser and
  ordinary service restarts; activity does not extend it.
- Logout ends the represented browser session. Password or frontend-security
  rotation invalidates every session.
- Expiry, logout, replacement, revocation, or inability to establish current
  session validity conceals protected content before returning to login.
- Destructive restore or recovery cannot revive an expired, revoked, or
  superseded session.
- An authenticated task deep link returns to the same board-plus-panel route
  after login.
- The browser never receives agent, admin, or Action credentials and stores no
  protected task data in persistent browser storage.

The product is desktop-focused. The board, cards, panel, login, logout, Load
more, and banners remain keyboard operable with visible focus and appropriate
announcements at the supported minimum desktop viewport.

## Authority-preserving evolution

The browser consumes backend-produced facts and guidance. It does not read
PostgreSQL, call Asana, decide workflow legality, compute canonical next
actions, or treat displayed data as mutation authority.

Any future state-changing control requires a separately approved product design
and a named backend command with an exact principal, replay identity,
current-view/concurrency fences, and governed audit behavior. The service
reasserts legality. There is no generic canonical-content save, arbitrary row
patch, browser-owned workflow engine, or generic mark-completed action.

Future structured editing, cooking-planning concepts, mobile layouts, and
frontend access to Marco-only administrative actions are unapproved. They must
not be inferred from the current UI or implemented as extensions of this
read-only contract.
