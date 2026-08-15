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
a private, read-only board with active-title search, task detail, background
refresh, warnings, and shared-password authentication.

It does not provide completed-task browsing, history, body/ingredient/content
search, editing, task creation, drag-and-drop, workflow or recovery commands,
administrative intervention, cutover controls, or direct PostgreSQL or Asana
access.
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

## Active title search

The board has a persistent read-only search over the full current active corpus,
using the same eligibility rules as board cards rather than only the cards already
loaded in the browser. Search matches the current canonical Dish title only, as a
case-insensitive literal substring. It does not search body/content, prior
revisions, completed or archived dishes, or Asana.

The backend bounds each request to 50 results and reports when more matches exist.
Each result contains the canonical Dish UUID and current title plus project and
section labels for disambiguation. Selecting a result performs the ordinary fresh
detail read and opens `/dishes/<stored-dish-uuid>/<decorative-title-slug>`; the
browser does not maintain a search index or a second task identity. Search failure
is isolated from board refresh and leaves the last usable board available.

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

Healthy active projection is omitted. While an external-projection epoch is enabled, delay, failure, drift, uncertainty, or unavailability is shown only as non-authoritative abnormal projection information. After rollback burn disables external projection, retained projection/drift/reconciliation rows are forensic history and do not produce task-health warnings. Projection never changes workflow legality.

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

### Proposed state-changing admin design

**Status: design proposal for review. It does not authorize implementation or
activation.** The current frontend remains read-only until this design is
separately accepted and an implementation change establishes the required
backend, transport, security, replay, audit, and browser contracts.

The first mutation-capable admin surface should expose a small set of
Marco-facing intents rather than shell commands or generic legal-action
execution:

- **Answer Human Review.** Present the exact current durable question, ranked
  choices with A recommended, and Other. Submit one selected choice or Marco's
  exact Other text against the exact review subject. The backend semantic path
  is the existing Human Review branch currently surfaced through
  `review-approve`; the browser does not construct governed before/after values
  or low-level continuation commands. When a structured choice already carries
  an exact governed authorization, the service records that bound authorization
  as part of the authoritative decision path.
- **Supply Evidence.** Submit Marco's exact factual answer to the exact current
  Evidence dependency using the existing `supply-evidence` semantic path.
  Evidence answers are factual input, not Human Review approval or a generic
  authorization mechanism.
- **Decide an exact semantic proposal.** Approve or reject the exact proposal
  currently presented, using the existing `review-approve` / `review-reject`
  authority. Approval and application remain separate durable actions. The
  normal backend path may mechanically apply the exact approved immutable
  bundle after fresh revalidation when safe; the browser does not expose a
  second generic "apply whatever is approved" control.
- **Replace one outstanding invocation.** Offer the human intent "replace this
  run" using the existing exact-run `kill` semantics. The UI does not ask Marco
  for lease, owner, run, operation, or request identifiers. The service binds
  the action to the exact outstanding principal represented by the submitted
  current view and fails closed if that subject changed.
- **Continue deterministic recovery.** Offer one recovery action only when the
  backend has established one safe deterministic continuation. This requires a
  named frontend-facing backend command that delegates to the existing
  recovery/abandonment/reconciliation authorities and returns the resulting
  fresh state. Do not expose advanced `recover --outcome` assertions or a menu
  of low-level repair commands as ordinary browser choices. If a real human
  choice is required, present that semantic choice first and let the service
  perform deterministic follow-on steps after the answer.

The first mutation-capable frontend does **not** expose bulk kill, raw
`repair-destination`, `discard`, `abandon-operation`,
`reconcile-abandonment`, `reopen`, `resolved`, raw
`authorize-governed-change`, backup/restore, migration/cutover controls, or a
command executor. Those remain diagnostics, exceptional recovery, operational,
or future-design concerns unless a later product decision explicitly promotes
one into a normal semantic intent.

### Mutation principal, replay, and stale-view contract

A mutation-capable implementation must preserve the existing frontend trust
boundary rather than handing admin credentials to the browser:

- The browser continues to authenticate with the frontend session cookie. The
  browser never receives the admin bearer token or an agent/Action credential.
- Mutation authorization is an explicit allowlisted frontend capability, not an
  automatic consequence of possessing any read session. The exact caller
  principal is the server-validated frontend-admin caller class plus the current
  `FrontendPrincipal.session_id`; expired, revoked, replaced, or
  security-generation-invalid sessions cannot mutate.
- Each logical mutation carries one canonical UUID request identity generated
  for that user action. A transport retry reuses the same request identity and
  same authenticated principal. Exact reuse replays the first authoritative
  outcome; incompatible reuse conflicts. The browser does not invent a second
  idempotency model or manufacture fresh identities to escape uncertain work.
- Every actionable presentation carries an opaque server-issued current-view
  identity that binds the durable subject and the material authority facts on
  which the offered intent depends. The browser treats it as opaque. Mutation
  admission must verify that identity against fresh authoritative state before
  executing; a stale or changed subject fails closed.
- A stale-view failure refreshes the relevant Dish/review state. If the subject,
  consequence, available choices, or exact proposed bundle changed, the browser
  requires a new human confirmation rather than silently resubmitting the old
  intent against the refreshed object.

The current-view identity is a concurrency/admission fence, not workflow
authority. The service still reasserts the command's legal transition from
fresh authoritative facts.

### Mutation result and audit contract

Every admitted frontend mutation must be durably attributable and must return
fresh state suitable for the next human decision:

- Audit records identify the authenticated frontend principal/session, permanent
  request identity, semantic command, exact durable target, submitted
  current-view identity (or its durable digest), authoritative outcome, and any
  deterministic internal substeps that were actually committed.
- Human Review/Evidence records preserve the exact selected structured choice or
  Marco text according to the owning workflow contract. Audit must not persist
  browser secrets merely to prove attribution.
- A successful mutation returns the authoritative post-mutation outcome and a
  freshly derived view/continuation; it must not return a continuation computed
  from pre-mutation state.
- If a deterministic follow-on step cannot complete safely, preserve every
  already-committed durable fact and return the remaining blocker explicitly.
  Do not broaden the approved subject or guess a different recovery path.
- If the response is lost, exact replay of the same request returns or settles
  the same authoritative logical outcome rather than executing a second
  mutation.

Frontend wire shapes, endpoint names, current-view token encoding, and
implementation-specific command adapters remain implementation work after this
design is approved. The product requirement is the semantic intent and the
principal/replay/fence/audit behavior above, not a particular HTTP path or JS
module layout.

Future structured editing, cooking-planning concepts, mobile layouts, and any
additional frontend access to Marco-only administrative actions remain
unapproved. They must not be inferred from this design or implemented as
extensions of the current read-only contract.
