# Dish private frontend implementation contract

**Status: staged implementation contract. Delivery Stages 0 and 1 are available after explicit authorization. Delivery Stage 3 read-core/local observation and Delivery Stage 4 read-only detail/deep-link candidate implementation are explicitly authorized behind the loopback-only non-production PostgreSQL observation boundary; Delivery Stage 2 and production/private Stage 3/4 HTTP/browser activation remain readiness-gated, and later stages remain conditionally specified.**

This document defines how to realize the approved product in
[`frontend.md`](frontend.md). The product behavior and authority outcomes in `frontend.md` are
normative. This document owns every implementation change required because of the frontend,
regardless of whether that change lands in browser code, the private service, application/query
services, PostgreSQL queries or storage, deployment, or tests.

Read [architecture index](architecture/index.md), [`runtime-contract.md`](runtime-contract.md),
[`database-backend.md`](database-backend.md),
[`database-backend-imp.md`](database-backend-imp.md), and
[`database-backend-migration.md`](database-backend-migration.md) as governing authority contracts.
This implementation must preserve their shared authority, admission, shutdown, and database
invariants. This document is the additive governing contract for the frontend-specific caller,
session, routes, read models, and PostgreSQL support, including additions not present in the older
pre-frontend runtime surface. It does not alter the contracts of existing CLI, admin, agent, or
Action callers and does not require reopening or amending the database design, implementation, or
migration documents.

This contract is normative for authority, security, externally observable behavior, retry safety,
consistency, and interoperability. Unless an exact format or mechanism is required so independent
components can agree, internal storage, transaction, fencing, cryptographic packaging, and browser
state-machine choices remain implementation-local. Examples of possible mechanisms are non-normative
unless this document explicitly says otherwise.

## 1. Ownership boundary

The existing PostgreSQL authority, workflow-policy ownership, projection authority, and audit
semantics remain unchanged. Frontend Stage 1 owns the complete delivery slice required for the
approved experience, not merely the browser and HTTP surface.

This document owns all work introduced because the frontend needs it, including:

- browser application and private frontend routes;
- board, task-detail, authentication, and session DTOs;
- a dedicated private frontend OpenAPI document and schema-checked client;
- a dedicated frontend session principal and exact route scope;
- application and query services created for the board and task-detail experience;
- PostgreSQL queries, read models, indexes, views, storage, or migrations required solely to support
  the frontend contract;
- cursor, pagination, ordering, refresh, rendering, projection-presentation, and error mechanics;
- deployment configuration and frontend-specific acceptance, performance, and security tests.

These are frontend implementation requirements even when they are implemented below the HTTP layer.
They must conform to existing database authority and transaction invariants, but they are not added
to, tracked in, or used to reopen the database design and implementation documents.

Adding a frontend requirement does not transfer authority to the route handler, query projection, or
browser. Handlers call bounded application/query services; frontend-driven database support remains
read-oriented for canonical task and workflow data and cannot reproduce workflow policy or become a
second authority model. Frontend-owned session, throttling, security-audit, or other explicitly
permitted support state remains non-authoritative and cannot modify or become task or workflow
authority.

## 2. Listener, admission, and trust boundary

The service continues to expose exactly the existing private and Action listener surfaces. The
frontend must not create a third externally reachable listener or independently admitted server. The
private browser origin uses a hostname distinct from the Action/Funnel origin; port separation alone
is insufficient because browser cookies are not port-scoped.

Every frontend request must:

1. cross the same process-wide request-admission and graceful-drain boundary as existing private
   requests;
2. validate the request authority/Host against the configured canonical private HTTPS origin before
   authentication or routing; trusted proxy authority metadata is accepted only under the deployment
   trust contract, and untrusted forwarded Host headers are ignored;
3. execute only on the private listener;
4. be unreachable and undiscoverable from the Funnel/Action listener.

The unauthenticated routes introduced and owned by the frontend are limited to:

- the login shell and the static JavaScript, CSS, fonts, and images required to render it; and
- the login submission route.

This frontend-owned limit does not remove, reclassify, or take ownership of the pre-existing
unauthenticated read-only Action schema route that the governing runtime contract exposes on the
private and Action listeners. That route remains outside the frontend product, contains no frontend
routes, and keeps its existing Action-schema authority and synchronization rules.

Those frontend-owned resources contain no secret, session material, protected task data, environment details, or
runtime configuration beyond public frontend boot information. The login submission performs only
bounded shared-password verification and session creation after admission. Every protected HTML,
session-bootstrap, and task-data route validates a live server-managed session before protected
application or domain-database work. Logout is the sole narrow exception to live-session authorization:
it validates the presented lifecycle cookie and CSRF proof only to revoke or confirm cleanup under the
idempotent contract below and can never return protected data or grant another route. Session-store
access required solely to validate or revoke a session is part of authentication, not domain work. An
unauthenticated protected deep link redirects to login with an opaque same-origin
return target; successful login restores that target. Confirmed logout, expiry, or revocation clears
protected client state before returning to the login shell; an unresolved logout keeps the same state
concealed under the explicit retry outcome defined below.

The exact HTTP or ASGI integration mechanism is an implementation choice provided these invariants
hold. FastAPI may be used only inside this boundary; it is not a new service, authority layer,
listener, or shutdown lifecycle.

Frontend board, task-detail, guidance, and canonical-content routes never call Asana in any
environment. Imported Asana identifiers and downstream projection evidence are read only through
PostgreSQL-owned records.

## 3. OpenAPI and route ownership

The existing Action schema remains a separate bounded product:

- `/openapi/action.json` remains owned by the Action command registry and its existing
  synchronization tests;
- frontend routes do not appear in the Action document;
- a separate private frontend schema, such as `/openapi/frontend.json`, describes only approved
  frontend routes and requires a live frontend session to retrieve;
- the frontend schema declares one cookie-session security scheme using the
  `__Host-dish_frontend_session` cookie; login is explicitly unauthenticated, every protected
  board/detail/session-bootstrap operation requires that session scheme, and logout requires both the
  session scheme and the required `X-Dish-CSRF` header parameter;
- every frontend API operation declares the required `X-Dish-Frontend-Contract` request and response
  header behavior plus the exact JSON request and response media types;
- session bootstrap is a protected GET with no request body and the exact response object defined in
  Section 4; it does not create, replace, extend, or otherwise mutate session authority;
- the frontend schema has its own synchronization and typed-client checks;
- default framework documentation and schema routes are disabled unless explicitly bound to the
  private frontend contract;
- the Funnel/Action listener exposes neither frontend routes nor the frontend schema.

Exact route spelling and framework integration are implementation choices. Route scope is not.

Frontend APIs use bounded UTF-8 JSON with closed generated schemas. Routes that accept JSON require
`application/json`; unsupported media types return HTTP 415 `media_type_unsupported`. Malformed JSON,
duplicate or unknown members, wrong types, or values outside declared bounds return HTTP 422
`request_invalid` before application dispatch. Governing listener-level limits remain owned by the
shared runtime contract.

The browser never follows redirects for frontend API calls and never accepts a response whose final
URL differs from the requested same-origin API URL. A redirect or unexpected final URL on any
protected or lifecycle request is a security failure: protected state is cleared or remains concealed
and the operation surfaces `session_unavailable` or `logout_unavailable` as applicable.

Response-validation failures are classified by what the browser can safely establish:

- an unreadable or incompatible session-bootstrap response, or an unreadable/incompatible 401 or 403
  response to a protected API request, surfaces `session_unavailable` and clears protected state;
- a network failure or unreadable/incompatible 5xx response from board, pagination, or detail preserves
  the last compatible view as `service_unavailable` while the local fixed-expiry boundary remains valid
  and no session-invalidity outcome has been established;
- a syntactically successful board, pagination, or detail response with an incompatible contract
  version, media type, status, or schema, and any unreadable or incompatible non-redirect task-data 4xx
  response outside the session-security cases above, is rejected as local `contract_mismatch`, preserves
  the last compatible state, stops committing task-data responses, and offers the full reload path;
- an otherwise readable error whose HTTP status and registered code do not match the checked-in OpenAPI
  mapping is local `contract_mismatch`;
- logout failures keep content concealed and surface `logout_unavailable`;
- an unreadable or incompatible login error remains on the login shell, grants no session authority,
  and surfaces `service_unavailable`; other login failures surface the applicable login, update, or
  service error.

No unreadable response is interpreted as proof that a session remains valid or that a mutation
committed. Redirect or unexpected-final-URL failures and protected 401/403 or session-bootstrap
security failures take precedence over version, media-type, status/code, and schema mismatch handling:
they clear or conceal protected state even when the same response is also contract-incompatible.

The browser build and private API share one checked-in frontend contract version. Every frontend API
request carries exactly one bounded canonical `X-Dish-Frontend-Contract` value. For a supported request
version, every API response carries exactly one equal selected-version value. A missing, duplicate,
conflicting, malformed, or unsupported request value fails before application dispatch as
`client_update_required`; that response uses the stable minimal error envelope and carries exactly one
server-supported version value rather than pretending the unsupported request version was selected.
The browser treats any missing, duplicate, conflicting, malformed, or unequal response value as an
update-required condition before visible state is committed and does not depend on parsing the response
body to reach the reload path. Every API success or error response uses exactly one compatible
`application/json` content type. Board, pagination, detail, login, session-bootstrap, and logout
responses are accepted only when their version, media type, status/code pairing, and schema are
compatible with the loaded client.

On mismatch, the browser stops task-data requests, preserves the last safe view only while the current
session remains demonstrably valid, and offers a full-page reload of the no-store shell and current
immutable assets. Session expiry, revocation, or inability to establish validity clears protected
state. The contract does not require the server to maintain a seven-day multi-version browser
protocol or retain every retired asset for the full session lifetime.

Login, session bootstrap, logout, and the minimal authentication/error envelope remain simple and
stable enough that an incompatible client can clear protected state and reach the reload path. Exact
release sequencing is implementation-owned, but a deployment must publish its immutable assets before
advertising their version and must never serve a client/API combination that can misinterpret
protected data.

## 4. Authentication and session contract

Stage 1 uses one environment-specific shared password. The private service verifies it and exchanges
it for an opaque server-managed browser session. That session principal authorizes only the approved
frontend read routes and its own session-bootstrap/logout lifecycle; it has no command, admin, agent,
Action, or canonical-mutation scope. The browser never receives or stores backend bearer credentials
used by agent, admin, or Action clients.

The session contract is normative:

- the configured shared-password verifier is an Argon2id hash; plaintext server configuration is
  prohibited;
- login accepts one bounded JSON password string. The complete request body and password value are
  bounded before password-hash verification; malformed or oversized requests fail as `request_invalid`;
- the decoded password string is compared exactly: no trimming, Unicode normalization, case
  conversion, or browser-side canonicalization is permitted;
- password verification uses the Argon2id library's constant-time verification path; the verifier
  parameters are pinned in checked-in environment configuration, meet an approved minimum memory/time/
  parallelism security floor and a bounded operational-cost ceiling, and are validated at startup.
  Exact tuned values are deployment-owned, but weakening the approved floor requires explicit security
  review rather than an unnoticed configuration change;
- five failed attempts from one trusted transport-derived peer identity within fifteen minutes block
  that source for fifteen minutes, while a separate global ceiling of thirty failures per fifteen
  minutes rejects further attempts. A peer identity may come only from the direct socket or
  authenticated proxy metadata under the deployment trust contract, never from untrusted forwarded
  client-address headers. Rate-limit state persists across service restarts or is enforced by the
  trusted admission layer so restart cannot reset the protection. If the required limiter cannot
  establish the pre-verification state needed for a login decision, login fails closed as
  `service_unavailable` before password verification. A failed verification is not returned as
  `login_invalid`, and a successful verification does not create a session, until any required limiter
  update for that outcome is durably accepted; inability to do so returns `service_unavailable`.
  Throttling disclosure reveals only that retry is temporarily delayed and the approved remaining
  delay, never whether any submitted password was otherwise valid or close to valid;
- every admission, authority, contract-version, request-media, whole-body-size, closed-schema,
  same-origin/fetch-metadata, singleton-security-value, and throttling check that does not require
  password-hash verification occurs before Argon2id work, so an ineligible, cross-origin, malformed,
  oversized, ambiguous, or throttled request cannot force expensive password verification;
- a rejected login does not disturb a still-valid preexisting session. If a login succeeds or its
  outcome is ambiguous, the initiating browser context does not reveal or commit protected state until
  it has bootstrapped the session represented by the currently shared cookie. A committed replacement
  invalidates responses, CSRF state, and other security state belonging to the superseded session
  before any tab may accept further protected responses under it. The exact cross-tab coordination,
  request fencing, and client-state mechanism are implementation choices;
- successful login replaces any valid frontend session represented by a preexisting cookie as one
  committed security outcome, creates a fresh opaque session token with at least 256 bits of
  cryptographic randomness, retains no recoverable copy of the bearer token, and commits its required
  security-audit record or fails closed without issuing a new cookie. A one-way verifier or another
  equivalent non-recoverable lookup representation may be used. It sets a bounded
  `__Host-dish_frontend_session` cookie with `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, and
  no `Domain` attribute. The successful response emits exactly one non-conflicting `Set-Cookie`
  occurrence for that frontend cookie name; it never relies on browser first- or last-occurrence
  selection. Every same-origin tab reconciles to the replacement before accepting further protected
  responses. Presented cookie values are bounded and syntax-checked before expensive
  cryptographic or session-validation work. On login, one unambiguous cookie occurrence that reaches
  frontend admission but is malformed, outside the frontend cookie-value bound, expired, revoked, or
  unknown is never treated as a valid session and does not prevent a correct password from replacing it
  with a fresh cookie; duplicate or otherwise ambiguous cookie occurrences remain rejected as defined
  below. This does not override the governing whole-request and header-admission bounds. Any cross-tab
  session signal is ephemeral and contains no task or credential data;
- security-bearing request values are ambiguity-intolerant. A request never selects a first or last
  value from duplicate frontend-session cookie occurrences, `Origin` headers, or `X-Dish-CSRF`
  headers. Duplicate or otherwise ambiguous session cookies map to `auth_required` for protected or
  session-bootstrap requests, to `logout_unavailable` for logout while protected state remains
  concealed, and to `request_invalid` for login before password work or session change. Missing,
  duplicate, malformed, conflicting, or non-canonical Origin values map to `origin_rejected`; missing,
  duplicate, malformed, or conflicting logout CSRF values map to `csrf_rejected`;
- the session persists across browser restarts for a fixed seven days measured from successful login;
- activity does not slide or extend that deadline;
- the cookie and server-side session validity record both use a fixed `604800`-second lifetime, and the
  server enforces expiry rather than trusting only cookie expiry;
- session validation and revocation state persists across ordinary service restarts, retains no
  recoverable bearer token, and contains only bounded frontend-security metadata needed to establish
  issue and fixed expiry, validate or revoke the session, enforce security rotation, and prevent
  restore from reviving invalid sessions. Its exact schema and lookup representation are
  implementation-local, and it contains no protected task data or credentials for another caller
  surface;
- logout revokes only the current server-managed session. A logout transition initiated in any tab is
  propagated to every active same-origin browser context within the two-second ceiling in Section 6,
  and each active context conceals and clears protected task/view state within that ceiling. A frozen,
  suspended, or otherwise
  unscheduled context clears before it can reveal protected content again. The propagation mechanism is
  implementation-local and carries no task or credential data;
- password rotation invalidates all existing sessions. Password and other global frontend
  session-security rotations are ordered against concurrent session creation so a login verified
  under superseded password or security state cannot create a session that remains valid after the
  rotation;
- expired, invalid, missing, revoked, rotation-invalidated, or restore-invalidated sessions fail
  closed and invalidate protected client state in every same-origin context sharing the cookie. Active
  contexts clear within the applicable Section 6 ceiling, and any context that could not run at the
  time clears before protected
  content can be revealed again. For protected and session-bootstrap admission, a missing, malformed,
  unknown, or otherwise unmatched credential maps to `auth_required`, fixed expiry maps to
  `session_expired`, and revocation or explicit security invalidation maps to `session_revoked`;
- cookie mutation and stale-response handling ensure that a delayed logout, expiry, revocation, or
  other older lifecycle response cannot make an older session authoritative over a newer successful
  login, revoke that newer session, or restore protected state under the older one. Preserving the newer
  cookie where the browser protocol permits is one possible approach; if response ordering leaves an
  older or unusable value in the cookie jar, that value remains server-inert and protected content stays
  concealed until the browser validates a current session or logs in again. The exact safe
  cookie-cleanup and response-ordering strategy is an implementation choice;
- login and logout are POST-only, require the accepted `application/json` request media type defined
  above, reject duplicate or unknown object members, and require one unambiguous same-origin `Origin`
  value plus the required fetch-metadata validation. The login body is exactly
  `{"password": <string>}`, the logout body is exactly an empty object, logout requires the
  per-session CSRF proof in `X-Dish-CSRF`, and no GET request changes authentication state, extends
  session expiry, or rotates the session credential. A
  successful login or logout is HTTP 200 with exactly an empty JSON object under the lifecycle schema;
  login success additionally issues the new cookie, while logout success applies race-safe cookie
  handling without affecting any newer session;
- after login or session restoration, the protected GET session-bootstrap operation returns exactly
  `{"expires_at": <RFC 3339 UTC timestamp>, "remaining_seconds": <integer>,
  "csrf_proof": <ASCII string>}`. `expires_at` is the fixed server-authoritative expiry instant;
  `remaining_seconds` is bounded to `0..604800` and is never greater than the whole seconds actually
  remaining when the successful response is authorized for release; `csrf_proof` is bounded,
  header-safe ASCII with at least 128 bits of effective forgery resistance. The proof uses frontend-specific
  security material distinct from agent, admin, Action, password, and route-identity support; the client
  keeps it only in memory, returns it on logout, and the server verifies it without timing-dependent
  comparison. The CSRF design supports ordinary service restart, browser restart, multiple tabs,
  invalidation when supporting security state changes, and safe resolution of a lost logout response.
  Derivation versus stored verification state, exact token framing, and proof-retention mechanics are
  implementation choices. The client establishes one non-sliding local expiry boundary from both the
  supplied absolute instant and the request-start-relative `remaining_seconds`; wall-clock rollback,
  suspension, activity, or implementation clock choice cannot extend visibility beyond either supplied
  bound. The exact clock and fencing mechanism is implementation-local, and the client clears
  protected state at that safe boundary even while offline;
- password provisioning and rotation require a minimum of sixteen characters under one checked-in
  counting rule used consistently by every provisioning and rotation path; that rule does not trim or
  normalize the value. They enforce the same maximum decoded-password bound accepted by the login
  schema, so a provisioned password can always be submitted through the approved login route. They
  reject defaults or equality with any agent, admin, Action, frontend
  session/CSRF security secret, or other configured secret, and store only the Argon2id verifier. The
  password is never embedded in source, URLs, logs, generated HTML, JavaScript configuration, cookies,
  service workers, or cacheable responses;
- Dish does not retain the submitted plaintext password in application-managed browser storage after
  login;
- agent, admin, and Action credentials cannot be used as frontend sessions;
- frontend sessions cannot be used on CLI/admin, agent, or Action routes.

A protected response must not reveal task data if session expiry, logout, replacement, password or
frontend session-security rotation, restore-induced invalidation, or another revocation becomes
effective before that response is committed to the browser. If the request can no longer establish a
valid session, the protected payload is withheld and the corresponding lifecycle outcome is returned;
inability to establish validity returns `session_unavailable`. Implementations may satisfy this with a
final validation, generation fence, request lease, transaction boundary, or another equivalent
mechanism. No such mechanism may slide expiry or become task/workflow authority.

When logout begins, the initiating tab immediately conceals and clears protected task data and stops
protected background refresh. Every other active same-origin context sharing the session does the same
within the two-second ceiling in Section 6 after observing the logout transition; a frozen or suspended
context clears before it can
reveal protected content on resume. The browser may retain only minimal, non-persistent lifecycle state needed to resolve or explicitly
retry logout, including the already issued CSRF proof when the chosen retry design requires it. That
state contains no task data, password, plaintext session token, authority usable on another route, or
content capable of restoring the protected view. Its exact shape and lifetime are implementation
choices subject to those limits, and task content remains concealed while completion is uncertain.

The logout flow is idempotent for the presented browser session. The logout operation returns success
when it can validate the presented lifecycle proof and either revoke the matching live session or
confirm that the same represented session is already expired or revoked. If cleanup has removed the
state needed to validate that proof, a subsequent lifecycle check that establishes there is no valid
session also completes the client-visible logout outcome; the server need not misreport an
unverifiable request as a successful authenticated operation. A lost successful response is therefore
safely resolvable. Network or server failure keeps protected content concealed, presents
`logout_unavailable`, and retries only on explicit user action. The current page never restores task
data automatically while that logout outcome remains unresolved. A later user-initiated navigation or
full-page reload may begin a new session-restoration attempt from a concealed shell; protected content
may reappear only after successful validation of the then-current session, and that restoration must
not be presented as proof that the earlier logout succeeded. The exact retry proof, temporary client
state, and cleanup mechanism are implementation choices, but they may not grant session authority,
expose protected data, or let a stale response affect a newer login.

The production frontend origin uses HTTPS only, and Dish exposes no plaintext frontend endpoint. If
the hosting platform provides an HTTP-to-HTTPS redirect, it remains outside Dish request handling and
accepts no credentials or protected data. The deployment enables HSTS at the trusted HTTPS termination
point. The configured canonical private origin is the sole accepted browser authority and cookie host,
is dedicated exclusively to Dish rather than shared with another application, and is not shared with
the Action/Funnel origin. The initial deployment uses one private browser/API origin with CORS
disabled. A separate origin requires an exact environment-specific allowlist and revised deployment
contract.

Every frontend response sends `X-Content-Type-Options: nosniff` and
`Cross-Origin-Resource-Policy: same-origin`. Frontend HTML responses, including login and protected
shells, also send `Cross-Origin-Opener-Policy: same-origin` and a restrictive `Permissions-Policy` that
disables browser capabilities not required by Stage 1. The content-specific CSP and referrer-policy
requirements are defined with rendering below.

Login responses, protected HTML, protected API responses, and session responses use
`Cache-Control: no-store`. Shared caches and browser back-forward cache must not retain protected task
content after logout or expiry. Versioned public static assets may use immutable caching because they
contain no secrets or protected data. Service workers are not used in Stage 1. Ordinary task-data
refresh and lifecycle operations surface any session invalidity they observe, but this contract does
not impose a separate active-view session-validation cadence beyond the approved refresh, fixed-expiry,
page-restoration, suspension-return, and explicit lifecycle rules. On page restore, including browser
back-forward restoration, the client revalidates the session before revealing protected content. A tab
returning from suspension first checks its local fixed-expiry boundary and then revalidates with the
server.

The session validation/revocation state and frontend CSRF/session-security material are frontend-owned
server configuration and storage. Session creation prevents fixation. Password rotation invalidates
every existing session; rotation or loss of other frontend security material invalidates every session
whose validity or CSRF proof depends on it. Expired or revoked records are cleaned up without extending
valid sessions. A destructive PostgreSQL restore or point-in-time recovery must not make an expired, revoked,
superseded, or otherwise invalid session usable solely because restored session rows or security
records describe it as valid. Task-data admission remains closed until the governing post-restore
readiness boundary can establish current session validity without trusting stale restored state.
Session creation or bootstrap may remain available only when its independent security outcome can be
established safely under that boundary; otherwise it also fails closed. Any session whose current
validity cannot be established requires a fresh login. The restore-safe invalidation or fencing
mechanism is implementation-local.
Session creation, logout revocation, and global invalidation commit with their required security-audit
records or fail closed. Audit records capture successful and failed login outcomes, throttling, logout,
expiry, and security-driven session invalidation without recording plaintext passwords or session
tokens.

## 5. Presentation API and read models

### 5.1 Board bootstrap and section pagination

The approved discovery product is section-based board browsing. The presentation API provides one
bounded board bootstrap and independent continuation for each section.

One board-bootstrap operation returns the active registry and the first page for every section. The
browser does not issue one initial request per section. Independent section operations are used only
for **Load more** after bootstrap.

The board read model must:

- return the active logical section registry in authoritative order;
- use positive bounded server-defined first-page and continuation-page sizes that are validated at
  startup and reported where the browser needs them;
- return the first bounded page of ordinary or isolated, incomplete tasks for every section;
- represent empty active sections explicitly and treat zero active sections as a successful empty
  board;
- support one bounded continuation cursor per section;
- exclude completed and retired tasks server-side while keeping otherwise eligible `isolated` tasks visible;
- order tasks predictably by a server-owned normalized-title key, then Dish task UUID as the deterministic tie-breaker;
- use one checked-in versioned normalization-and-comparison contract consistently for task-title
  ordering and project/section-label comparison, without exposing its internal keys to the browser.
  The checked-in contract defines normalized equality and the deterministic ascending comparison used
  by the server and includes deterministic test vectors.
  Changing its behavior changes the frontend contract version because it can alter card order or label
  disambiguation. A project or section label whose normalized value is blank is invalid. Equal normalized section
  labels require the project label; if normalized project-plus-section paths are still not unique,
  board bootstrap fails as `board_configuration_invalid` rather than displaying ambiguous columns;
- require no stored manual card position or reorder authority in Stage 1;
- avoid per-task Asana reads and per-task application-service or workflow-policy loops;
- include logically placed tasks even when they have no current or historical operation;
- use PostgreSQL logical project and section identities, with display names as labels;
- show the project label when needed to disambiguate equal section labels;
- keep completion, destination, operation, Verification, lease, hold, recovery, abandonment,
  succession, and projection as separate facts;
- treat cards as staleable factual summaries rather than current-view or mutation authority.

The board bootstrap captures one evaluation time and reads canonical registry, membership, card, and
available projection-presentation facts through one coherent read operation. Lease expiry, projection
delay, and other time-derived presentation use that same evaluation time. Serialization and browser
formatting do not keep the canonical read transaction open.

The board-bootstrap DTO contains:

- one opaque board snapshot identity whose equality has defined meaning: it remains equal for
  equivalent active-registry order/labels, effective first-page size, first-page card
  identity/order/visible fields, section continuity identities, `next_cursor` presence, and notices,
  and changes when any of those presentation inputs changes;
- the effective first-page size;
- an ordered `sections` collection;
- a required bounded `notices` collection, which is empty when no returned first-page card has an
  active attention condition.

Each section entry contains:

- one stable browser-facing route identity;
- section label;
- project label when needed for disambiguation;
- one opaque section continuity identity;
- the first page of card DTOs;
- an opaque `next_cursor` exactly when more eligible cards remain, otherwise `null`. A section with
  eligible cards returns between one and the effective first-page size cards; an empty first page is
  terminal and therefore has `next_cursor: null`.

Within one accepted board state, section route identities are unique. Every returned card's section
identity equals the containing section's current route identity, every current normalized task route
identity appears at most once across the accepted first pages and retained continuation pages, and a
card contains no duplicate attention code. A violation is local `contract_mismatch`; the client does
not display or merge the invalid response.

A card DTO contains only:

- one stable task route identity for detail lookup;
- title;
- containing section route identity;
- one factual workflow-status object containing either an active operation and optional phase or the
  approved **No active operation** state;
- zero or more approved attention codes.

Ordering keys, database revisions, authority generations, and other internal consistency inputs remain
server-owned and are not browser DTO fields. Browser-facing task and section route identities are
non-raw, non-sensitive, bounded, stable for routine deep links, and scoped to the correct object type
and environment. They must not expose a database UUID, Asana GID, other external identifier, or secret;
the contract does not mandate encryption, a stored alias table, or any other particular encoding.
They are validated as data and never interpolated into SQL, filesystem paths, templates, or logs.
A malformed, wrong-type, wrong-environment, or out-of-bound route identity fails as `request_invalid`;
a well-formed task identity that resolves to no task fails as `task_not_found`. Every accepted legacy
or alternate representation is normalized by the server to one current
browser-facing identity before board, detail, pagination, URL, or client state is committed. Current
card/detail responses return that current identity so a task not already present on a loaded board page
can still be reconciled without duplicate panels or history entries.

The Stage 1 attention-code registry is limited to:

- `isolated` — **ISOLATED**;
- `lease_attention` — **Lease needs attention**;
- `verification_attention` — **Verification needs attention**;
- `hold_active` — **On hold**;
- `recovery_required` — **Recovery required**;
- `abandonment_active` — **Abandonment active**;
- `succession_active` — **Succession active**;
- `projection_abnormal` — **Asana projection issue**.

One checked-in versioned attention registry defines the exact backend predicate, approved label, and
banner severity for each code and is synchronized with the generated schema and tests. No unregistered
state qualifies for an attention code. Changing any predicate, label, or severity changes the frontend
contract version and requires an explicit frontend-contract update. The current contract requires these
distinctions:

- `isolated`: `DishTask.existence_state = 'isolated'`; the task remains board-eligible when the
  other eligibility facts pass and the marker is never inferred from projection drift or labels;
- `lease_attention`: a lease is expired, invalid, or contested; an ordinary healthy active lease does
  not qualify;
- `verification_attention`: Verification is failed, disputed, or awaiting human review; ordinary
  pending or in-progress Verification does not qualify;
- `hold_active`: a named active hold exists;
- `recovery_required`: a named unresolved recovery requirement exists;
- `abandonment_active`: an active abandonment fact exists;
- `succession_active`: an active succession fact exists;
- `projection_abnormal`: the mapped projection state is `delayed`, `failed`, `drifted`, `unknown`, or
  `unavailable`; healthy `current` and intentionally absent `not_configured` do not qualify.

The browser maps codes to approved labels and never infers attention from arbitrary fields. Card
indicators use the registry order shown above. Attention codes are a closed, contract-versioned set:
an unknown code rejects the response as local `contract_mismatch` rather than being silently omitted.
Do not expose a generic authoritative `blocked` value.

The presentation API uses three separate consistency concepts:

1. an opaque board snapshot identity for one accepted bootstrap or refresh response;
2. an opaque section continuity identity plus continuation cursor for **Load more**;
3. a backend-internal task-detail current-view identity.

The browser treats all three as opaque. It may compare snapshot and continuity identities only for
equality. Equal identities do not suppress response validation, notices, or first-page reconciliation.
Task-detail current-view tokens remain internal and never enter the browser DTO.

A **Load more** request contains the section route identity and its current cursor. The continuation
read captures one evaluation time and returns its page cards and notice contributions from one coherent
bounded read. The response contains the current section route identity, repeats the section continuity
identity, returns between one and the effective continuation-page size cards, returns another cursor
exactly when more cards remain, and includes the bounded notices attributable to that returned page.
An accepted current cursor never produces an empty nonterminal page.

Cursor behavior is normative at the outcome level:

- cursors are bounded, opaque, tamper-resistant, and do not reveal query or authority details;
- they are bound to the section, eligibility predicate, ordering, page size, page boundary, and all
  other inputs that affect the returned page;
- retrying the same request after a lost response is safe and does not skip or consume cards;
- malformed, tampered, wrong-type, or wrong-environment cursors fail as `cursor_invalid`;
- structurally valid cursors that have expired, whose server-side handle was retired by bounded cleanup,
  or whose bound board state is no longer compatible fail as `cursor_stale`;
- inability to validate an otherwise well-formed cursor because its required cursor service or store is
  temporarily unavailable fails as `service_unavailable`, not as invalid or stale;
- either cursor error resets only the affected column to a fresh first page before **Load more** is
  re-enabled;
- the browser never silently rebases or merges incompatible pages.

Cursors may be sealed stateless values, server-side handles, or another equivalent implementation.
Every cursor has an explicit positive server-enforced maximum validity period no longer than the fixed
frontend session lifetime. Any server-side cursor resources are cleaned up no later than that bound.
Persistence, cryptographic packaging, and the shorter configured lifetime are implementation choices
provided the observable guarantees above hold and ordinary retry does not lose cards.

The bootstrap and continuation operations are bounded by configured limits for section count, page
size, response size, query work, and execution time. They need not scan or count the entire eligible
board merely to serve paginated pages or warnings. Notice contributions cover only cards returned by
that response, and displayed counts are derived only from the currently accepted contributions defined
in Section 8. No scan or aggregate over unloaded tasks is required solely to produce warning counts. A
response never silently
omits required sections or returned-page cards. Unresolved label/path ambiguity or another invalid board configuration returns
`board_configuration_invalid`; a known configured capacity bound returns `board_capacity_exceeded`;
transient inability to establish canonical board state returns `service_unavailable`.

Stage 1's omission of legal-action list fields or filters does not remove, defer, narrow, or replace
the backend's required set-oriented legal-action selector or its equivalence tests.

### 5.2 Task detail

Opening a card fetches a fresh canonical detail view. The detail route returns only a task that is
non-retired, incomplete, and placed in the active registry. A missing task returns `task_not_found`;
a completed, retired, or out-of-registry task returns `task_ineligible`.

The internal detail service may use current-view tokens and technical identifiers required for
consistency, but the browser DTO excludes them.

The detail query, rendering, sanitization, and response are bounded by configured canonical-body,
render-work, render-output, response-size, query-work, and execution-time limits that accommodate the
governing maximum canonical task document. A known input, output, or complexity bound violation fails
as `detail_capacity_exceeded` without partial or silently truncated content. A transient deadline
expiry or inability to perform the bounded work because a dependency required to establish canonical
detail state is unavailable maps to `service_unavailable`; a projection-evidence outage that can be
classified safely under Section 7 instead produces a successful response whose projection state is
`unavailable`. An unexpected invariant failure maps to `internal_error`. The detail service captures
one backend evaluation time and reads canonical content, every authoritative factual input, and any
durable projection evidence available in PostgreSQL inside one short read-only transaction and
coherent database snapshot. Lease expiry, projection delay, and every other time-derived fact use that
same evaluation time. Any optional read-only projection-presentation dependency outside that snapshot
is sampled once under the same classification rule as board bootstrap. After the transaction closes,
the service derives disclosure items, advisory guidance, notices, projection presentation, and
rendered content only from those captured immutable facts and presentation inputs. The browser-facing task-detail DTO
includes:

- the current browser-facing task route identity, allowing any accepted legacy deep link to normalize
  before visible state or browser history is committed;
- canonical title plus one required closed `body_presentation` union: either
  `state: sanitized_html` with exactly one backend-rendered `html` field, or
  `state: plain_text_fallback` with exactly one inert `text` field; the two payload fields are mutually
  exclusive, the fallback branch requires exactly one `render_rejected` notice targeted to the
  selected task, the sanitized branch forbids that notice for the task, and raw canonical body source
  is retained server-side and is not sent to the browser during normal rendering;
- logical project and section display labels;
- canonical destination label when present;
- one factual workflow-status object containing `state: active_operation`, an operation label, and an
  optional phase label, or `state: no_active_operation` with the approved **No active operation**
  label;
- zero or more human-disclosure items from the registered Stage 1 categories `lease`,
  `verification`, `hold`, `recovery`, `abandonment`, and `succession`. Every item uses the same closed
  wire shape: exactly a bounded category code, an approved bounded label, and an optional bounded
  plain-text `detail` string. One centralized versioned disclosure registry defines the permitted label and source facts for each
  registered category, and the browser never parses the detail or treats it as authority. Changing a
  disclosure category, source predicate, or approved label changes the frontend contract version. Lease detail may describe only a human owner/role label, lease-state label, and
  human-readable expiry; Verification detail may describe only a state label and approved summary;
  hold, recovery, abandonment, and succession detail may describe only their approved kind/state label
  and approved summary. Disclosure items render in category order `lease`, `verification`, `hold`,
  `recovery`, `abandonment`, then `succession`, with repeated items in backend-defined stable order. No
  item contains raw IDs, request evidence, executable instructions, or category-specific object
  members;
- one separately named, explicitly non-authorizing next-step advisory;
- an abnormal projection object only when the state is `delayed`, `failed`, `drifted`, `unknown`, or
  `unavailable`, containing only that state, a plain-language message, and an optional human-readable
  observation time.

For the selected task, every active non-projection attention predicate requires at least one
corresponding disclosure item in its matching category: lease, Verification, hold, recovery,
abandonment, or succession. `projection_abnormal` requires the abnormal projection object. The detail
response may also include approved factual disclosures that are useful but not attention states, such
as a healthy active lease or ordinary Verification state. Missing required attention detail rejects the
response as local `contract_mismatch`.

The browser DTO excludes:

- canonical `allowed_actions`;
- raw current-view, request, replay, run, or audit identifiers;
- raw execution/effect evidence;
- raw administrative continuation details;
- raw reconciliation diagnostics;
- infrastructure, database, exception, or internal-model details;
- raw external aliases, projection mapping identifiers, or reconciliation evidence.

Board-bootstrap, section-pagination, and detail success envelopes always include the bounded `notices`
collection defined in Section 8; it is an empty collection when the response contributes no active
condition.
Every board, pagination, detail, session, error, and notice object has a closed generated schema with
`additionalProperties: false` or its exact equivalent. Attention and disclosure categories are closed,
contract-versioned sets. An unknown attention code, disclosure category, notice code, member, or
required state rejects the response as local `contract_mismatch`; the client never hides an unknown
required human-disclosure fact and continues as though the response were complete. All required DTO
fields are validated before display. An incompatible board response preserves the
last compatible board; an incompatible detail response preserves the last compatible panel for the
same selected task. No partial required object is displayed.

The next-step advisory DTO contains exactly:

- one bounded stable advisory code from a non-sensitive namespace that contains no raw or task-specific
  identifier or user-authored text, is opaque to the browser, is not displayed, and never controls
  presentation or behavior; unfamiliar values remain inert and do not invalidate an otherwise
  compatible advisory;
- one plain-English message, using **No next step is currently available** when no further condition
  can be described from current canonical facts;
- `perspective: workflow`; and
- `invokable_by_frontend: false`.

An unknown disclosure category, missing required detail field, or unknown required state invalidates
the detail response and preserves the last successful compatible panel; raw fields are never passed
through as a fallback.

The backend-owned factual/current-workflow service produces the advisory from the same canonical
workflow facts used by the authority layer. The code and text may describe the next required
condition, responsible role, or waiting reason, but cannot claim that the frontend principal may
invoke it. The DTO contains no raw command, principal-specific `allowed_actions`, agent impersonation,
or private administrative continuation and cannot be generated by browser policy.

### 5.3 Canonical and rendered content

The backend owns the only Stage 1 renderer and sanitizer. On the normal branch it renders the
canonical source into sanitized HTML before constructing the browser DTO; on the defined failure branch
it produces only the inert plain-text fallback below. It records a stable renderer/version identity in
server-side evidence and tests; that identity is not displayed and need not be sent to the browser.
The browser does not receive raw body source during normal rendering, does not render Markdown or
arbitrary HTML itself, and inserts only `body_presentation.html` from the `sanitized_html` branch into
the task-content container. Titles, labels, advisory text, disclosure text, banner text, the
`plain_text_fallback` branch, and every other non-body string are inserted only as text, never as HTML.

The renderer and sanitizer versions are pinned dependencies with bounded recursion and input/output
limits. Their checked-in emitted-element allowlist is limited to safe semantic representations of
paragraphs, line breaks, headings, emphasis, strong text, ordered and unordered lists, list items,
blockquotes, horizontal rules, inline and preformatted code, tables, and links. The exact HTML element
selection within those categories is renderer-version implementation detail and may not introduce an
additional semantic or active-content category. The only permitted attributes are link `href`,
`target="_blank"`, and a `rel` value containing exactly the `noopener` and `noreferrer` tokens. Links may use only `http`, `https`, or same-origin relative references.
Every accepted relative reference is resolved and normalized server-side against the fixed canonical
private-origin application root rather than the current board or task deep-link URL. It is emitted in a
normalized same-origin form whose meaning is independent of the current route; root-relative and
canonical absolute forms are both permitted. A result that is not same-origin is rejected.
Protocol-relative URLs are rejected. Any link using `target="_blank"` must also contain exactly those two `rel` tokens.
Remote images and other embedded active content are rendered as safe text or links rather than
fetched inline. No other element or attribute is emitted. Scripts and event attributes are rejected.
Unapproved raw HTML is escaped as visible text rather than silently discarded, and unsafe link
destinations are neutralized while preserving their human-readable
label. Every frontend HTML shell sends a
Content Security Policy equivalent to `default-src 'self'; script-src 'self'; style-src 'self';
img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none';
frame-src 'none'; worker-src 'none'; media-src 'none'; manifest-src 'none'; frame-ancestors 'none';
form-action 'self'`, plus `Referrer-Policy: no-referrer`. The document title remains a fixed generic
Dish label and never includes task, section, project, warning, or login-state text; Stage 1 emits no
browser notifications, third-party analytics, telemetry, or automatic external-resource requests. Every dynamic non-body text field is wrapped in bidirectional-isolation semantics, and the
rendered-body or plain-text-fallback container is isolated as one bounded content region, so Unicode
direction controls cannot reorder surrounding application chrome. Tests cover scripts,
event attributes, dangerous URLs, malformed markup, embedded HTML, renderer-version reporting, and
deterministic correspondence between canonical and rendered content.

Ordinary escaping of unapproved raw HTML or neutralization of an unsafe link is a successful
`sanitized_html` rendering outcome, not a renderer failure. If the pinned renderer or sanitizer cannot
produce a valid allowed result for a canonical body that remains within every configured capacity
bound, the service returns the `plain_text_fallback` branch containing the canonical source as inert
plain text, not HTML-escaped markup. The panel inserts it only through `textContent` or an equivalent
text-only DOM operation and raises one grouped `render_rejected` banner. A canonical-body, render-work, render-output, fallback-output, or response-size bound violation
returns `detail_capacity_exceeded` instead of the fallback; a transient execution deadline or
dependency failure returns `service_unavailable`. Unsafe content never becomes executable and does not
require replacing an otherwise usable board.

### 5.4 History

Stage 1 exposes no history API, timeline, historical-content view, projection history, or client-side
event merge.

## 6. Refresh and reconciliation

The client performs bounded automatic background refresh by reissuing the approved board and detail
reads. Scheduling, cancellation, and in-memory cache mechanics are implementation choices subject to
service-load and acceptance tests. The deployment-configured active-view refresh interval is positive
and no greater than 30 seconds. While the private service is reachable and can establish session and
canonical read outcomes, an active visible tab therefore discovers server-side expiry, revocation,
rotation, and canonical board/detail changes within 30 seconds unless an earlier protected request or
same-origin session signal discovers them first. A domain-read failure that still establishes a valid
session follows the last-safe-view rule; inability to establish session validity follows
`session_unavailable` and clears protected state. A local logout or successful replacement
login clears or supersedes protected state in every active same-origin tab within 2 seconds; a frozen
or suspended tab revalidates before revealing protected content. A server-triggered signal or other
additional browser-visible transport is not authorized by this flexibility and must be added to the
frontend contract and its applicable schema before use.
Protected board, detail, notice, and session-bootstrap state is never persisted in local storage,
session storage, IndexedDB, Cache Storage, or another browser application store. At most one board
refresh is authoritative at a time: overlapping work is prevented, cancelled, or sequenced so an older
response cannot overwrite a newer accepted state. Detail responses are accepted only for the currently
selected task and current request sequence. Duplicate concurrent **Load more** requests for one section
are prevented or deduplicated. Logout, expiry, or session revocation invalidates every in-flight
protected response before it can restore cleared client state. Failed background refresh attempts use a bounded retry policy that prevents tight loops and avoids
synchronized retry bursts. While the tab remains active and visible, the retry delay never exceeds the
approved 30-second active-view refresh ceiling; a successful refresh resets the policy. The exact
jitter and scheduling algorithm is an implementation choice. One manual retry bypasses the current delay without creating concurrent
refreshes. Authentication failures do not retry until a valid session exists.

Each automatic or manual board refresh reissues the board-bootstrap operation. The response is accepted only after complete schema
validation. Equal board or section identities never suppress notice or first-page reconciliation. The
response replaces the registry and first page
of every section atomically from the client's perspective. A section's additional loaded pages are
retained only when its opaque continuity identity is unchanged; a changed identity discards those
pages and their cursor. For Stage 3, section continuity binds the active authority generation, active
registry version/revision, section identity, frontend/query/normalization contract versions, and the
effective first- and continuation-page sizes. It deliberately does **not** promise a frozen task
snapshot across continuation requests. If tasks are inserted, removed, retitled, or reordered between
page reads, ordinary keyset-pagination boundary effects are acceptable for this operational board and
may be refined later if real usage shows a usability problem.

Refresh reconciliation must preserve the product rules:

- retain the last successful board while refreshing;
- refresh the first page of every column from one successful bootstrap response;
- discard additional loaded pages for an affected column when pagination is no longer trustworthy;
- require **Load more** again after such a reset;
- after a stale or invalid cursor, discard only the affected column's additional pages and cursor,
  retain its last compatible first page, trigger one board-bootstrap refresh, and re-enable
  **Load more** only after that refresh supplies the current section and a fresh cursor;
- accept a **Load more** response only if its section continuity identity still equals the current
  column identity, its returned current section route identity equals the column's current route
  identity, and every returned card section route identity equals that current identity. If the server
  normalizes an accepted legacy section identity to a different current identity, discard the page and
  perform one board bootstrap so the registry and column identity are replaced atomically; otherwise a
  mismatch discards the response and resets that column. A temporary transport or
  `service_unavailable` load-more failure preserves the already loaded cards and re-enables the
  control for retry. A `request_invalid` load-more outcome preserves compatible loaded cards, discards
  the rejected continuation request and cursor, and performs one fresh board bootstrap. If the same
  request remains invalid after that bootstrap, **Load more** stays disabled for that column and the
  banner offers a full-page reload; the client never repeats the rejected request automatically;
- move a task to its new authoritative section without retaining a duplicate;
- remove newly completed or retired tasks;
- while the panel is open, fetch a fresh detail view after each successful board refresh;
- keep the last successful panel content with a banner when detail refresh temporarily fails but the
  session and board remain safe;
- keep the panel open when the selected task remains eligible and never let an older detail response
  replace a newer selection or newer detail state;
- close the panel, normalize the route to the board, and trigger one board refresh when detail reports
  `task_not_found` or `task_ineligible`;
- preserve the board's horizontal scroll position, each retained column's vertical position, and the
  open panel's scroll position when their underlying region remains compatible; when a moved focused
  task must be revealed, scroll only enough to bring the new focused location into view;
- clear recovered-condition warnings automatically;
- never use Asana fallback.

A changed section continuity identity identifies an affected column. The client must fail closed
rather than merge pages from different continuity identities.

## 7. Aliases and projection state

Imported Asana identifiers and downstream projection evidence remain separate service/read-model
concepts:

- `external_aliases` records source identifiers and provenance but is not exposed in the Stage 1
  browser DTO;
- `projection` reports downstream representation and reconciliation evidence and is exposed only
  when abnormal information is needed by the approved product.

The presentation API maps durable projection evidence into exactly these frontend states:

- `not_configured`;
- `current`;
- `delayed`;
- `failed`;
- `drifted`;
- `unknown`;
- `unavailable`.

The mapping contract is:

- `not_configured`: projection is intentionally absent for the environment;
- `current`: the latest required projection is durably applied and no unresolved drift exists;
- `delayed`: projection is pending or behind the backend-owned required freshness objective without a
  terminal failure; the frontend does not invent its own timer threshold;
- `failed`: the latest required projection has a terminal, blocked, or uncertain outcome requiring
  intervention;
- `drifted`: durable reconciliation evidence shows the downstream representation differs from the
  required projection;
- `unknown`: projection should be assessable but evidence is insufficient or internally inconsistent;
- `unavailable`: projection evidence cannot currently be read because its service or store is
  unavailable.

The mapping from backend evidence to those states is centralized and tested. When canonical task,
eligibility, and workflow facts remain readable but projection evidence cannot be read, the board or
detail response remains successful and uses `unavailable`; `service_unavailable` is reserved for a
dependency failure that prevents the service from establishing the canonical response or from safely
classifying projection availability. An imported alias never proves a current projection mapping or
freshness state. Projection state never changes canonical workflow legality.

Healthy `current` state is suppressed. Abnormal states use the global banner system and may identify
the affected task through its card or panel.

## 8. Errors and banners

Frontend APIs return stable machine-readable error codes with bounded plain-language presentation.
The browser does not infer authority or workflow behavior from error text.

Required error outcomes include:

- `auth_required`, `session_expired`, `session_revoked`, `session_unavailable`, and
  `logout_unavailable`;
- `login_invalid`, `login_throttled`, `origin_rejected`, and `csrf_rejected`;
- `media_type_unsupported`, `request_invalid`, and `board_configuration_invalid`;
- `task_not_found` and `task_ineligible`;
- `cursor_invalid` and `cursor_stale`;
- `client_update_required`;
- `service_unavailable`, `board_capacity_exceeded`, `detail_capacity_exceeded`, and
  `internal_error`.

Each non-success response has a closed bounded schema containing exactly:

- stable `code`;
- one bounded safe human-readable `message`;
- for `login_throttled` only, one positive bounded integer `retry_after_seconds`.

Adding another code-specific parameter requires an explicit frontend-contract and OpenAPI update.
Error messages are generic to the code and contain no task title/content, route identity, credential,
cursor, raw exception, infrastructure detail, or other sensitive dynamic value.

HTTP status mapping is stable and documented in OpenAPI. Authentication failures do not reveal whether
a guessed task, section, cursor, or internal identity exists. Unreadable or incompatible protected
security responses fail closed and clear protected state rather than being interpreted optimistically.

Every successful board-bootstrap, section-pagination, and detail response includes a bounded `notices`
collection, empty when no notice contribution is active. The required Stage 1 notice-code registry
includes every approved attention code plus
`render_rejected`; additional codes require an explicit frontend-contract update. For each notice code,
the checked-in registry fixes its severity and default plain-language message. A notice is one
per-instance banner contribution and contains exactly:

- stable `code`;
- the registry-defined `severity` (`warning`, `error`, or `information`);
- the registry-defined bounded safe human-readable `message`;
- one required `target` equal to
  `{type: "task", route_identity: <current task identity>}`.

For a board or pagination response, every notice target is a card returned by that same response and
the notice codes targeted to each card equal that card's complete attention-code set exactly; no
additional task target is permitted. For a detail response, every notice targets the current selected
task and task-targeted attention notices equal the complete active attention-predicate set captured for
that task, every non-projection attention notice has its required matching
disclosure item, `projection_abnormal` has its required abnormal projection object, and
`render_rejected` appears exactly when the plain-text fallback branch is used. A response never contains duplicate notice contributions for the
same code and task identity, and `render_rejected` never appears in a board or pagination response.

Distinct active conditions stack in the common banner area. The client maintains contributions from
the current accepted bootstrap, retained continuation pages, and open detail, deduplicates equal
code-and-task contributions, and groups each code into one displayed banner. While detail for a task is
successfully open, its complete attention-notice set supersedes card-derived attention contributions
for that same task; a resolved condition therefore disappears without waiting for the next board
refresh. If detail refresh temporarily fails, the last compatible detail contributions remain with the
last compatible panel. When the panel closes, current card contributions apply again. The displayed
affected-task count is the number of distinct current task targets for that code and is shown whenever
the count exceeds one. An ungrouped single-task banner may link to that task; a grouped banner does not
choose an arbitrary task target. Current lifecycle or request failures are separate banner conditions and are not counted as task
contributions. Repeated failures with the same error code and operation scope update one active banner
rather than stacking duplicates; a successful relevant operation clears that failure condition. When a
page or panel is discarded or a condition resolves, its contributions are removed. Neither server nor client scans unloaded pages solely to produce a
count. Unknown error or notice codes or structures reject the response as local `contract_mismatch`.

The client behavior for each stable code is checked in and tested. At minimum:

- authentication or session-invalidity outcomes clear protected state and return to login;
- `logout_unavailable` keeps protected state concealed, preserves no assumption that server revocation
  committed, and offers explicit logout retry or a fresh login path;
- `login_invalid` remains on the login shell, does not disturb a valid preexisting session, and uses
  the generic login-error message; `login_throttled` additionally prevents submission until the
  returned retry delay has elapsed;
- `origin_rejected` and `csrf_rejected` fail the lifecycle request without granting or restoring
  protected state;
- `task_not_found` or `task_ineligible` closes the affected panel, normalizes its route back to the
  board, presents the plain-language banner, and triggers one board refresh so any loaded card is
  reconciled rather than removed by browser inference;
- `cursor_invalid` and `cursor_stale` reset only the affected column through a fresh board response;
- a load-more `request_invalid` follows the fresh-bootstrap and reload behavior in Section 6;
- `board_configuration_invalid` displays no partial board, keeps the shell available, and requires
  corrected server configuration before retry can succeed;
- `client_update_required` stops task-data requests and offers a full reload;
- a task-data `service_unavailable` outcome preserves the last safe board or panel while the local
  fixed-expiry boundary remains valid and no session-invalidity outcome has been established;
- initial-load failure keeps the board shell visible with retry;
- rendering rejection uses the defined inert plain-text fallback when available;
- `board_capacity_exceeded` or `board_configuration_invalid` never displays a partial new board;
  during refresh the last compatible board may remain visible with the error banner while continued
  viewing is safe, and during initial load only the persistent shell and retry/configuration guidance
  remain;
- `detail_capacity_exceeded` never displays partial or truncated detail and preserves the last
  compatible panel for the same task when continued viewing is safe;
- `internal_error` follows the same no-partial-data rule as the affected board, detail, or lifecycle
  operation; a lifecycle `internal_error` never establishes, replaces, extends, or confirms session
  authority, while any preexisting authority is handled only by that operation's explicit failure rule;
- `contract_mismatch` is a client-local condition and is never emitted as a server API error code. It
  preserves the last compatible state, stops committing incompatible responses, and offers the full
  reload path.

Banner wording, grouping, and targeting remain presentation behavior. They must not become a second
workflow-policy engine or mutation surface.

## 9. Deep links and browser state

The selected task identity is reflected in the URL. Reloading or revisiting the URL restores the
board and opens the selected task when it remains eligible.

The UI provides no dedicated **Open in new tab** control. Normal browser behavior may open a deep
link in another tab, where it renders the same board-plus-panel product.

The selected task identity is transmitted as the current bounded URL-safe, non-raw, non-sensitive
browser-facing route identity defined in Section 5.1. It is absent from the human-readable
presentation and does not expose a database UUID, Asana GID, other external identifier, or secret.
When an accepted legacy form is used, the server normalizes it to the current identity before visible
client state or browser history is committed, so old and current forms cannot create duplicate panels
or history loops. When authentication is required, the return target must match the closed board-route grammar: the
board root plus, at most, one syntactically valid frontend task route identity. It is a bounded, normalized
relative path on the same origin; arbitrary private API, login, logout, schema, or static-asset paths,
schemes, authority components, protocol-relative paths, dot-segment traversal, duplicate parameters,
fragments outside the approved grammar, and control characters are rejected and discarded without
preventing a valid login; that login lands on the board root. A valid target is restored only after a
successful same-origin login.


Opening a card from a board-only route creates one task-route history entry unless that task is
already selected. Selecting a different card while the panel is already open may replace or otherwise
coalesce task-route history, but it must not make UI close reveal an earlier task panel or create a
loop. If the panel was opened from the current board history entry, UI close returns to that board
entry; if the application was loaded directly at a task deep link, UI close replaces that route with
the board root. Browser Back and Forward reconcile the panel to the route: Back can close a panel
opened from the board and Forward can reopen the most recently selected eligible task. Login
restoration, task switching within an already open panel, and automatic refresh do not add history
entries. Invalid, missing, or ineligible routed tasks are normalized back to the board route after the
plain-language notice is presented.

Access and application logs never record concrete task or return-target paths. Route templates or
another non-sensitive route label may be used when route classification is needed. Logs redact cookies,
CSRF headers, password bodies, opaque session/cursor values, task titles and content, dynamic project,
section, operation, phase, advisory, disclosure, projection, and notice text, and any return-target
value. Diagnostic correlation may use generated request or trace identifiers, but no
such identifier is exposed as task authority or browser presentation. Browser-history entries and operating-system metadata surfaces populated from the document URL or
title receive only the opaque deep-link URL and fixed generic document title, never task titles,
section labels, warning text, or canonical content. This metadata rule does not claim to suppress the
ordinary on-screen application content from user-visible window previews or screen capture.

## 10. Accessibility and interaction mechanics

The board is an accessible named region and exposes a programmatic busy state during initial load and
manual retry without hiding the persistent shell. Each section is a labeled region with a heading,
loaded-task count, and an indication when more tasks are available; a section exposes its own busy
state during **Load more** without disabling unrelated columns. A zero-section board exposes an accessible
empty-state message. Horizontal scrolling is keyboard reachable. Cards and **Load more** controls use
native keyboard-operable controls. Card accessible names include the title and concise factual status without
repeating every banner; every attention indicator has its approved accessible label. **Load more**
announces the number of cards added. The panel is exposed as a named nonmodal dialog or equivalent complementary region, never as a modal
that hides the still-operable board from assistive technology. Its visible close control, Escape key,
and approved click-outside interaction close it under the browser-history rules in Section 9. It moves
focus into itself when opened and restores focus to the originating card
when that card still exists; otherwise focus returns to the containing column heading, or to the named
board region or programmatically focusable empty-state message when that column no longer exists. Background refresh never steals
focus: a focused card that remains present keeps focus, a moved card transfers focus to the same task
in its new column when that card is rendered; otherwise focus moves to the new or former column
heading, falling back to the named board region or zero-section empty state if no relevant column
remains. A removed card or reset page uses the same column-then-board fallback. Banners do not take focus automatically. Error banners use assertive alert semantics only
when immediate attention is required; warning and information banners use polite status semantics.
Text, controls, focus indicators, status lines, attention indicators, and banners meet the approved
desktop WCAG AA contrast targets. Banner severity is communicated by text and semantics rather than
color alone, and live-region
behavior must announce new conditions without repeatedly announcing unchanged refresh results.
Nonessential motion is not required; if used, it respects the user's reduced-motion preference and
never becomes the only indication of a state change. Login inputs have persistent labels, lifecycle errors use the common banner treatment and are also
programmatically associated with the form or affected control, and throttling exposes the remaining
retry delay as text.

## 11. Staged implementation, modularity, and review gates

The approved product Stage 1 is implemented as a sequence of bounded delivery stages. These are
implementation stages inside product Stage 1, not additional product stages and not permission to omit
any final requirement. The purpose is to make the product visible early, obtain design feedback before
expensive integration is locked in, and keep the codebase modular and tested from the first commit.

Each stage ends with all of the following:

- a runnable local deliverable rather than screenshots alone;
- the tests appropriate to that stage passing in the normal repository test command;
- a short stage note listing what is real, what is fixture-backed, what remains intentionally absent,
  and any known limitations;
- representative screenshots or recordings for the user-visible states added by that stage;
- a human review gate before the next affected user-visible stage proceeds, unless that review is
  explicitly waived.

Feedback that changes approved behavior is written into `frontend.md` and, where necessary, this
document before the implementation continues. An implementation agent must not silently reinterpret
feedback or hide a product change inside component code. Passing one delivery gate does not constitute
final Stage 1 acceptance.

### 11.1 Code organization from the first commit

The implementation begins with separated concerns and remains separated throughout. An equivalent
framework-specific structure is permitted, but the repository must preserve these logical boundaries:

- a minimal HTML application shell and boot entry;
- external style sheets divided into design tokens, base rules, board/layout rules, and component or
  feature styles;
- feature modules for authentication/session, board and pagination, cards, task detail, notices,
  refresh/reconciliation, routing/deep links, and accessibility behavior;
- API schemas and generated or schema-checked client code outside presentation components;
- service transport/serialization code separate from application/query services and PostgreSQL read
  support;
- test fixtures separate from production code, with fixture data visibly identified as non-canonical;
- unit, component, service/integration, and browser-test code organized by the behavior it verifies.

The application must not begin as one combined HTML/CSS/JavaScript file or one catch-all application
module that is split only after the board is complete. Application CSS is not embedded in HTML or
JavaScript. Application JavaScript is not embedded in HTML. If the selected framework co-locates
markup and behavior, it still uses small feature/component files and separate style files or style
modules; it may not collapse the entire board, panel, session, and notice system into one component.

Hand-written application, service, style, template, and test files target at most 250 logical source
lines and must be split before exceeding 350 logical source lines. Generated clients, checked schema
snapshots, migrations, and intentionally data-heavy fixtures are exempt, but generated files are not
hand edited and fixture size cannot be used to hide application logic. The repository test or lint
workflow reports files that cross the review threshold. Any temporary exception requires a documented
reason and must be removed before the next delivery gate.

Dependency direction remains explicit: presentation components depend on typed feature/application
interfaces; they do not import PostgreSQL access, raw transport clients, authentication storage, or
workflow-policy code. Feature modules do not create alternate global stores or duplicate the same
canonical response model. Shared helpers remain small and purpose-specific rather than becoming a
generic dumping ground.

Every stage includes formatting, linting, type or schema checking, and the relevant unit and
integration tests. Tests for behavior delivered in a stage are delivered in that same stage; they are
not deferred to a cleanup phase.

### 11.2 Readiness evidence gates before integrated implementation

These gates correct a deliberate limit of the document: a future-state contract can define required
behavior without proving that every required source fact, listener hook, and query already has an
unambiguous implementation path in the current repository. Passing prose review alone is not enough to
authorize an integration stage.

Delivery Stages 0 and 1 may proceed after explicit authorization because they create the modular shell,
test infrastructure, and fixture-backed design prototype without claiming real authentication or
canonical task data. Delivery Stage 2 remains blocked until Gate A passes. By explicit project
authorization, Delivery Stage 3's read-only PostgreSQL query, DTO, route-identity, cursor core, and explicit
loopback-only local observation harness may proceed behind a non-production boundary while Gate B is
refreshed, including reading the non-authoritative dark-launch database as an operational observation
surface. The local harness is development evidence, not Stage 3D activation. By separate explicit
project authorization, Delivery Stage 4's read-only detail, rendering/disclosure/advisory candidate,
and deep-link behavior may also be implemented and exercised only through that same loopback local
observation boundary while its Gate B map is refined. Gate B must still pass before either real-data
stage is exposed through production/private frontend HTTP/browser routes. Stage 5 and later remain
gated by the applicable accepted source map. A prior stage review cannot waive an activation gate or
authorize guessed semantics.

#### Gate A — complete contract and runtime readiness review

Before Delivery Stage 2 begins, the implementation owner produces a checked-in readiness packet and an
independent reviewer who did not author the implementation reviews it against the complete current
`frontend.md` and `frontend-imp.md`, not excerpts or selected sections. The packet must contain:

- a requirement-traceability table covering every normative section of both documents, with the owning
  delivery stage, implementation module or boundary, required test level, and unresolved dependency;
- an authentication/runtime map covering the existing private listener, shared admission and drain
  gate, canonical private origin, configuration, session persistence, password verification,
  throttling, CSRF, logout, restore invalidation, and frontend-specific audit ownership;
- an OpenAPI ownership map confirming that the existing Action generator and synchronization tests
  remain unchanged and that the frontend schema/client synchronization pipeline is new Stage 0
  frontend work rather than an assumed pre-existing capability;
- confirmation that the complete error, notice, session, DTO, browser-storage, accessibility, and
  deployment contracts have an implementation and test owner;
- every uncertainty, contradiction, or required supporting change found during the review.

The gate passes only when all material findings are resolved in code or in these contracts, or are
recorded as explicit blockers assigned to a later stage that does not yet begin. A reviewer may not mark
the gate complete merely because the proposed architecture appears plausible. Product behavior may
change only through an approved contract amendment; technical gaps remain frontend-owned work.

#### Gate B — code-grounded canonical-data and attention map

Gate B is a checked-in living source map, reviewed and extended immediately before each real-data
stage. Before Delivery Stage 3 is activated through the production/private frontend HTTP/browser surface, it must cover every field emitted by board bootstrap, section
continuation, card status, card attention, and board projection presentation. Before Delivery Stage 4
begins, it must additionally cover every task-detail, disclosure, next-step-guidance, rendering-input,
and detail projection field. For each mapped field the packet identifies:

- the exact PostgreSQL model/table, application/query service, or other canonical source;
- the exact selection predicate, join/cardinality rule, evaluation-time rule, and precedence when more
  than one durable fact exists;
- the frontend-owned query, read projection, index, view, migration, or application service that must
  be added when the current read surface does not provide the required result;
- the bounded-query and no-per-task-loop proof, including representative query-plan or performance
  evidence where required;
- the unit, integration, equivalence, and acceptance tests that prove the mapping.

The Stage 3 portion of the map must separately cover each Stage 1 attention code. Terms such as
**expired**, **invalid**, **contested**, **failed**, **disputed**, **awaiting human review**,
**recovery required**, **active abandonment**, and **active succession** may not be interpreted from
their English labels or guessed by the route, browser, or query author. Each term must resolve to a
named canonical predicate. If the current repository has no exact durable source for an approved
predicate, the implementation must do one of the following before that portion of the gate passes:

1. add frontend-owned read support or durable support state that preserves the governing authority
   model and is covered by the required tests; or
2. propose a targeted contract amendment for review.

It may not silently weaken, broaden, or substitute the predicate. The mapping is independently
reviewed against the current code and schema. Human review is required only when resolving a gap would
change approved product behavior; implementation-local support remains an engineering decision.

#### Gate C — stage authorization record

After Gates A and B, later delivery stages still proceed one at a time. Each stage packet records the
exact commit or build reviewed, the gates already satisfied, new dependencies discovered, tests run,
and the human review outcome. Discovery of a material unmapped dependency pauses the affected stage and
reopens the relevant readiness packet; it does not authorize improvisation or a retroactive claim that
the documents were implementation-ready.

### 11.3 Delivery Stage 0 — foundation and empty shell

Build the implementation skeleton before product behavior:

- establish the directory/module boundaries and file-size checks from Section 11.1;
- establish reproducible local build, development, lint, schema/type-check, and test commands;
- establish the frontend OpenAPI synchronization and typed-client pipeline without yet exposing task
  data in the UI;
- create the login shell and protected empty application shell with external HTML, CSS, and
  JavaScript/assets;
- establish unit/component test infrastructure and the browser-test harness that will be used by the
  final stage.

The reviewable deliverable is a runnable empty shell plus a short architecture map showing the module
and style boundaries. It is not a Kanban board and does not claim product completeness. The gate
confirms that the codebase is structured correctly before feature volume grows.

### 11.4 Delivery Stage 1 — fixture-backed visual prototype

Build the approved board experience against explicit local fixtures, without pretending those
fixtures are canonical backend data. Include representative states for:

- multiple columns, an empty column, and the valid zero-section empty board;
- compact cards, long titles, **No active operation**, every approved attention category, and
  **Load more** presentation;
- the fixed-width side panel with representative content, safe-rendering fallback, and next-step
  guidance;
- stacked and grouped top banners;
- login, loading, initial-error, and last-safe-view shells;
- the minimum desktop viewport and horizontal board behavior.

The gate is primarily a design review. The user can reject card density, spacing, labels, column
headers, panel organization, warning treatment, or interaction shape before real data integration is
completed. The stage ships component and accessibility tests for the fixture-backed behaviors.

### 11.5 Delivery Stage 2 — authentication and protected application shell

**Entry condition:** Gate A in Section 11.2 is accepted and has no unresolved authentication, listener, session, OpenAPI, or deployment blocker assigned to this stage.

Integrate the real private login/session lifecycle and protected shell while keeping task-data scope
bounded. Deliver:

- login, session bootstrap, expiry, logout, replacement login, cross-tab clearing, and deep-link
  return behavior;
- private-origin, cookie, CSRF, cache, CSP, admission, and OpenAPI security behavior;
- a protected board shell that can still use clearly marked fixture content until the next stage.

The gate reviews the actual login, logout, session-expiry, reload, and deep-link experience. Security,
API-contract, and browser-lifecycle tests required by Sections 2–4 land in this stage.

### 11.6 Delivery Stage 3 — real board vertical slice

**Entry condition:** Read-core implementation and the loopback-only local observation harness may proceed under the explicit isolated/local authorization in Section 11.2. Production/private HTTP/browser activation requires the applicable Gate A runtime boundary and an accepted Gate B board scope. The code-grounded source map must cover every activated board field and attention predicate; unresolved semantics remain omitted or gated rather than guessed.

Connect the board to the real frontend-owned board read model. Deliver:

- authoritative sections in order, including empty sections;
- the first bounded page of non-retired, incomplete tasks per section;
- deterministic title ordering, card status, attention indicators, and **Load more**;
- bounded pagination, cursor validation, identity normalization, and board snapshot/continuity rules;
- explicit empty, configuration-failure, capacity, and service-failure outcomes.

Task detail may remain a clearly marked placeholder in this stage. The gate lets the user review the
real data density, section labeling, card wording, ordering, and loading behavior before the detail and
refresh systems are added. Board/query, pagination, DTO, schema, performance-bound, and integration
tests land with this stage.

### 11.7 Delivery Stage 4 — real task detail and deep links

**Entry condition:** Gate B has been extended and independently accepted for every detail, disclosure, advisory, rendering-input, and projection field introduced by this stage. The explicitly authorized loopback-only local observation candidate may be implemented and exercised before that acceptance, but it does not satisfy or bypass the production/private entry condition and must leave unresolved fields omitted rather than guessed.

Deliver the complete read-only side panel:

- fresh coherent detail, canonical title/body presentation, factual status, disclosure items,
  abnormal projection state, and non-authorizing guidance;
- backend-owned rendering, sanitization, plain-text fallback, link policy, and CSP behavior;
- direct deep links, URL normalization, Back/Forward behavior, and panel close/focus restoration.

The gate reviews actual task readability and panel organization before refresh and failure complexity
is layered on. Detail, rendering, disclosure, route-identity, history-exclusion, and deep-link tests
land in this stage.

### 11.8 Delivery Stage 5 — refresh, continuity, warnings, and failure behavior

Deliver the dynamic behavior around the already-reviewed board and panel:

- automatic board and selected-detail refresh;
- moved, completed, retired, unavailable, and recovered-task reconciliation;
- safe retention or reset of loaded continuation pages;
- last-safe-view behavior, retry, service and session failure distinction, and contract mismatch;
- task attention contributions, banner grouping, projection abnormalities, and warning recovery;
- stale-response rejection and multi-tab/session invalidation behavior.

The gate uses controlled state changes and failures so the user can review whether the product remains
understandable and stable while data changes. Refresh, race, retry, failure, notice, and continuity
tests land in this stage.

### 11.9 Delivery Stage 6 — accessibility, hardening, and production-shaped integration

Complete the integrated product without adding new product behavior:

- keyboard and pointer operation, focus restoration, live regions, busy states, contrast, reduced
  motion, and the approved desktop viewport;
- bounded performance, capacity behavior, logging redaction, configuration validation, startup
  failure behavior, and deployment checks;
- final OpenAPI/client synchronization and the complete automated acceptance suite in Section 12;
- removal of prototype-only code, fixtures from production paths, temporary file-size exceptions, and
  known test debt.

The gate is the final human design walkthrough of the integrated application. Any product discrepancy
found here is corrected and re-reviewed before browser acceptance begins.

### 11.10 Delivery Stage 7 — committed Playwright browser-acceptance suite

Browser-driven UI acceptance is a required final implementation deliverable, not future work, and it
is delivered as a **committed, repeatable automated test suite**, not a one-time manual or agent-driven
walkthrough. The scenarios below are implemented as Playwright (or an equivalent browser-automation
framework already used in the repository) test files checked into the repository's test tree, runnable
through the normal repository test command (or an explicitly named companion command documented in
`testing.md`), and executed against the production-shaped local application rather than mocked
component boundaries.

A capable local agent, such as Claude, authors this suite and performs its first full run as the Stage
7 gate, but the suite's value is that it remains in the repository afterward as a standing regression
gate for every future change to the frontend — it is not disposable evidence of a single sign-off. CI
or repository test-runner integration for this suite follows the same conventions as the project's
other automated gates.

The suite covers at least:

- login, reload, seven-day session metadata, logout, expiry simulation, replacement login, and
  same-browser multi-tab behavior;
- empty board, empty section, representative populated board, **Load more**, and horizontal/keyboard
  navigation;
- opening, closing, refreshing, directly deep-linking, and navigating Back/Forward through task
  detail;
- long and unusual content, safe-rendering fallback, projection warnings, every attention category,
  grouped banners, and recovered warnings;
- moved, completed, retired, missing, and temporarily unavailable tasks;
- service failure, session failure, contract mismatch, stale cursor, and initial-load retry behavior;
- minimum desktop viewport, focus restoration, visible focus, live-region announcements, and basic
  accessibility inspection;
- browser console errors, failed network requests, unexpected redirects, unsafe storage, and obvious
  layout regressions.

The first full run records the tested build and configuration, scenario results, screenshots for the
principal states, console/network findings, and every deviation from the contracts. Defects are fixed,
the affected suite files are rerun, and the full suite is repeated green before sign-off. Stage 1
cannot be marked implementation-complete until this committed suite passes cleanly against the
production-shaped build or every remaining limitation is explicitly accepted by the human reviewer
and recorded as a contract amendment. After sign-off, the suite continues to run as part of the
repository's ordinary test gates, not only at this one Stage 7 checkpoint.

## 12. Implementation acceptance

Final Stage 1 implementation acceptance is granted only when automated and production-shaped evidence
demonstrates all of the following. Work may proceed stage by stage under Section 11, but final
completion additionally requires the committed Playwright browser-acceptance suite in Section 11.10
passing against the production-shaped build.

### Delivery process and maintainability

- every delivery stage produces the runnable review packet, tests, and human review gate required by
  Section 11;
- Delivery Stages 2 through 7 have not begun before their Section 11.2 readiness gates passed;
- the checked-in Gate A traceability/runtime packet covers both documents in full and has an independent review record;
- the checked-in Gate B source map was accepted before each applicable real-data stage and identifies and tests the exact canonical source and predicate for every board, detail, projection, and attention field, with no unresolved guessed semantics;
- the application begins and remains separated into the approved shell, style, feature, API, service,
  data-support, fixture, and test boundaries rather than one monolithic board implementation;
- hand-written files comply with the Section 11.1 size thresholds or have no unresolved temporary
  exception at the next delivery gate;
- no stage carries deferred tests for behavior that the stage claims to deliver;
- fixture-backed prototypes remain isolated from production authority and are removed from production
  paths before final acceptance;
- the final production-shaped build passes both the complete automated suite and the committed
  Playwright browser-acceptance suite, with recorded evidence and reruns after fixes, and that suite
  remains checked in and runnable as a standing regression gate afterward.

### Product and authority

- the board shows every active logical section in authoritative order, including empty sections;
- board pages contain only non-retired, incomplete tasks in their authoritative section;
- task order is deterministic by the server-owned normalized-title rule and Dish task UUID tie-breaker;
- Stage 1 exposes no global search, completed-task view, history, mutation, administrative, or
  cutover-control route;
- cards and detail remain factual read models and never expose canonical `allowed_actions` or create a
  second workflow-policy path;
- the checked-in attention predicates are proved at their boundaries: a healthy active lease and
  ordinary pending/in-progress Verification do not create attention, while each registered attention
  condition creates exactly its approved code, label, severity, disclosure/projection detail, and
  banner contribution;
- frontend list, detail, guidance, and canonical-content reads never call Asana;
- imported aliases never establish projection freshness, every durable projection evidence class maps
  to the exact frontend state in Section 7, healthy/current state is suppressed, and projection
  evidence unavailability remains a successful canonical response when it can be classified safely;
- all frontend-driven backend and PostgreSQL support remains additive and does not alter canonical
  database authority or existing caller contracts.

### Board, pagination, and refresh

- one bounded bootstrap returns all active sections and the first page for each without per-task
  application-service or policy loops;
- empty boards and empty sections are valid success states;
- first-page and continuation-page sizes are positive and bounded; `next_cursor` appears exactly when
  more cards remain, and neither bootstrap nor continuation returns an empty nonterminal page;
- retrying a continuation after a lost response does not skip or duplicate cards;
- malformed, tampered, or mis-scoped cursors produce `cursor_invalid`; expired or incompatible
  current-state cursors produce `cursor_stale`; the Stage 3 cursor candidate is stateless and therefore
  has no cursor-store availability dependency; cursor errors reset only the affected column through a
  fresh first page;
- moved tasks appear only in the new section, completed or retired tasks disappear, task-detail
  `task_not_found`/`task_ineligible` closes and reconciles the panel through a board refresh, a normalized
  section identity is accepted only through a fresh bootstrap, and stale in-flight responses cannot
  restore removed or superseded state;
- warning counts are derived only from current accepted task contributions, never require a scan over
  unloaded tasks, and never claim an unloaded task;
- board/page notices target only cards in that response and exactly mirror each card's attention codes;
  detail notices target only the selected task and represent its complete current attention set plus
  any rendering fallback, detail supersedes stale card
  contributions for that task, repeated conditions are deduplicated and grouped with a truthful
  client-computed count, and discarded or resolved contributions are removed;
- section route identities are unique, cards are contained by their declared section, canonical tasks
  are not duplicated across retained pages, and attention codes are unique per card;
- equal board snapshot identities correspond to equivalent registry and first-page presentation state,
  and changed presentation inputs change the identity;
- configured limits or unresolved label ambiguity fail explicitly rather than silently omitting or
  displaying ambiguous data;
- the normalization-and-comparison, attention, and disclosure registries are checked in with
  deterministic tests and cannot change visible behavior without a frontend contract-version update.

### Detail and rendering

- opening a card retrieves fresh coherent detail rather than trusting the card as current authority;
- the side panel returns exactly the approved canonical content, factual state, disclosure items,
  abnormal projection presentation, and non-authorizing advisory guidance; every active attention
  notice has its required matching disclosure or abnormal projection object;
- task-detail current-view identities remain backend-internal;
- canonical rendering is backend-owned, sanitized, bounded, deterministic for its renderer version,
  and protected by the approved CSP and link rules;
- renderer rejection produces the inert plain-text fallback when safe, otherwise the panel fails closed
  while the board remains usable;
- raw source, executable content, technical identifiers, private administration details, and
  unapproved audit or reconciliation data are not disclosed.

### Authentication and browser security

- exactly the approved login shell/assets and login submission are unauthenticated; protected routes
  require a valid frontend session;
- the session principal cannot call command, admin, agent, Action, or other pre-existing caller routes;
- successful login creates a fixed non-sliding seven-day server-managed session, survives ordinary
  service restart, and does not expose backend bearer credentials; protected GET session bootstrap
  returns exactly `expires_at`, `remaining_seconds`, and `csrf_proof` without mutating or extending the
  session;
- Argon2id configuration floors and ceilings, pre-verification request checks, per-peer/global
  throttling, generic failure disclosure, throttling persistence across ordinary restart, and equality
  between provisioning and login password bounds are tested under production-shaped load;
- login replacement remains safe when its response is lost or delayed; logout clears the current
  browser session only, remains safe after a lost response, and stale logout or authentication
  responses cannot revoke or restore a newer session;
- password or frontend security rotation invalidates every dependent session, destructive restore
  cannot make an expired or revoked session valid again, and a protected response whose session becomes
  invalid before release withholds its payload;
- the browser clears protected state at the fixed local expiry boundary even while offline; while the
  private service is reachable, expiry, revocation, rotation, or canonical refresh changes are
  discovered by an active visible tab within the 30-second ceiling, including during failure backoff;
  local logout or replacement login reconciles active same-origin tabs
  within 2 seconds, and a resumed tab revalidates before revealing content;
- cookies, Origin/CSRF handling, HTTPS, same-origin deployment, cache controls, CSP, logging redaction,
  and no-service-worker rules meet this contract;
- contract-version mismatch stops task-data interpretation and offers a full reload without requiring a
  permanent multi-version browser protocol.

### API, errors, and interoperability

- Action and frontend OpenAPI documents remain separate and synchronized with their actual routes;
- frontend OpenAPI declares the cookie-session scheme, unauthenticated login, protected operations,
  and logout's CSRF header requirement per operation;
- JSON media-type, singleton contract-version header, supported-versus-unsupported version response,
  request-validation, redirect rejection, status/code pairing, and response-schema behavior match the
  normative contract, including security-failure precedence and the distinction between session
  security failure, ordinary task-data service failure, unreadable non-security 4xx responses, and
  local contract mismatch;
- error and notice schemas are closed, bounded, stable, use the exact registered fields and codes,
  contain no sensitive dynamic values, and do not make the browser infer authority;
- unknown attention, disclosure, notice, member, or required response states fail closed as local
  `contract_mismatch`; no unknown human-disclosure fact is silently omitted;
- task and section route identities are bounded, non-raw, non-sensitive, stable for routine use,
  correctly typed, normalized to one current identity, and never displayed as technical information;
  malformed or mis-scoped identities fail as `request_invalid`, while a well-formed unknown task
  identity fails as `task_not_found`;
  legacy identities cannot create duplicate client state, and login return targets accept only the
  closed same-origin board-route grammar without open redirects; the contract does not require a
  particular encoding or stored alias system;
- cursor validity and any server-side cursor resources have bounded lifetime and cleanup;
- ordinary deployment never serves a browser/API combination that can misinterpret protected data.

### Accessibility and interaction

- the approved minimum desktop viewport supports the non-wrapping horizontal board and fixed-width,
  single-scroll, tab-free nonmodal panel;
- cards, **Load more**, login, logout, retry, close, Escape, outside-click close, and banners are
  keyboard or pointer operable as applicable with visible focus;
- board/task URL history, direct deep-link close, Back/Forward restoration, legacy-route normalization,
  and the absence of a dedicated open-in-new-tab control match `frontend.md` Section 3 and this
  document's Section 9;
- focus restoration, moved/removed-card fallback, busy states, live regions, warning semantics, and
  horizontal navigation follow Section 10;
- WCAG AA contrast is met, color or motion is never the sole warning signal, and any nonessential
  animation respects reduced-motion preferences.

No acceptance evidence may rely on an implementation-specific mechanism where another safe mechanism
could satisfy the same observable guarantee.

## 13. Frontend-owned deployment and operational contract

All deployment, configuration, and operational work introduced by Stage 1 is owned by this frontend
implementation contract, including:

- the read-only frontend principal, shared-password verifier, fixed seven-day sessions, logout,
  invalidation, throttling, restore safety, and security audit;
- login, session bootstrap, protected-route authentication, same-origin checks, CSRF protection,
  request/response validation, and failure behavior;
- the separate private frontend OpenAPI document and preservation of existing Action schemas and
  caller surfaces;
- the private HTTPS origin, secure cookie behavior, cache controls, immutable assets, CSP, rendering,
  sanitization, logging redaction, and browser-storage controls;
- listener admission, canonical Host validation, trusted-proxy handling, and graceful-drain coverage;
- PostgreSQL queries, read models, indexes, views, or other support introduced solely for the frontend,
  while preserving coherent read-only snapshots and canonical authority boundaries;
- bounded board/detail performance, cursor retry safety, stable route identities, and explicit
  capacity and availability behavior;
- frontend-specific monitoring using non-sensitive aggregate metrics only.

The exact framework, query organization, session store, cursor representation, route-identity
encoding, cryptographic packaging, cache structure, polling interval within tested limits, and
component architecture remain implementation choices. They may not alter the approved user behavior,
authority, confidentiality, retry safety, or interoperability outcomes.

These requirements preserve the governing shared authority contracts, but this document is the
additive authority for the frontend-specific access path and remains the sole location for
frontend-driven work. No amendment to the database design, database implementation, or database
migration documents is required. Existing CLI, admin, agent, and Action contracts remain unchanged.
This contract does not change PostgreSQL authority, workflow-policy ownership, completion semantics,
projection authority, or cutover authority.
