# Frontend Gate A readiness packet

## Status

**Authoring review complete; Gate A is not passed.**

This packet was prepared against the current `frontend.md`, `frontend-imp.md`, service runtime,
PostgreSQL model set, deployment examples, frontend OpenAPI document, and fixture frontend. Gate A
still requires an independent reviewer who did not author the implementation. It also has the
material readiness findings listed below; Delivery Stage 2 must not begin until they are resolved and
the reviewer records acceptance in `frontend-gate-a-review.md`.

Prepared against the repository state containing Delivery Stages 0 and 1A–1F. The pre-database
implementation-local runtime decisions are recorded in `frontend-stage2-runtime-decisions.md` and
`../frontend/contracts/stage2-security-contract.json`. The pending PostgreSQL production migration is
treated as unfinished. This packet does not assume that currently checked-in
PostgreSQL models are already the deployed production authority.

## Evidence inspected

- Product and implementation contracts: `docs/frontend.md`, `docs/frontend-imp.md`.
- Runtime authority map: `docs/architecture.md`, `docs/runtime-contract.md`.
- Listener and drain path: `dish_service/http.py`, `dish_service/__main__.py`.
- Existing service configuration and bearer authentication: `dish_service/config.py`,
  `dish_service/auth.py`.
- Existing request/restore gates: `dish_service/maintenance.py`,
  `dish_service/request_coordinators.py`, and restore modules under `dish_service/`.
- Current PostgreSQL authority models and read surface: `dish_pg/models.py`,
  `dish_pg/stage3_models.py`, `dish_pg/stage5_models.py`, `dish_pg/read_model.py`.
- Deployment configuration: `deploy/systemd/service.env.example` and service unit examples.
- Frontend contract/build: `frontend/openapi/frontend.openapi.json`, `frontend/src/`,
  `frontend/tools/`, and `frontend/tests/`.
- Python dependency pins: `requirements.txt`, `requirements-test.txt`.

## Material findings blocking Gate A

| ID | Finding | Required resolution before Stage 2 |
|---|---|---|
| A-01 | Canonical-origin, no-forwarded-trust, and request-order decisions are now closed in `frontend-stage2-runtime-decisions.md`; no validating configuration or admission code exists yet. | Add validated environment configuration and the singleton-header admission component, then prove the recorded order and rejection behavior. |
| A-02 | Route ownership and the frontend security-header policy are now closed in the Stage 2 runtime decisions; the service still has no implementing routes, static/HTML delivery, or response writer. | Add frontend routing under the existing private `DishHTTPServer`; prove the Action listener returns 404 for every frontend route and schema path. |
| A-03 | Only bearer-token authentication exists. There is no Argon2id verifier dependency, shared-password provisioning/rotation path, cookie parser, session principal, CSRF proof, or browser lifecycle service. | Add pinned Argon2id support and the complete frontend authentication application boundary. |
| A-04 | No frontend session, security-generation, throttling, or frontend-security-audit persistence exists in the checked-in PostgreSQL model set. | Finalize and migrate frontend-owned support tables against the target PostgreSQL authority before Stage 2 integration. |
| A-05 | The required destructive-restore/PITR invalidation fence is not designed. Restoring session rows could otherwise revive superseded authority. | Select and test a restore-safe generation/fence whose current value cannot be rolled back solely by restoring PostgreSQL. |
| A-06 | Frontend request ordering, singleton values, cookie behavior, and response headers are now specified in the Stage 2 runtime decisions, but the service still has only the generic JSON writer. | Add a frontend response writer and request parser separate from existing agent/admin/Action envelopes. |
| A-07 | The Stage 0 frontend OpenAPI file and generated client are checked only inside `frontend/`; the service neither serves nor synchronizes that document. | Add private-session-protected schema serving and a repository synchronization test while leaving the Action generator unchanged. |
| A-08 | The browser has only fixture shell state. It has no session bootstrap, safe fixed-expiry calculation, concealment boundary, logout retry state, or cross-tab invalidation. | Implement the Stage 2 browser session modules and browser acceptance cases after server readiness is complete. |
| A-09 | The current dependency set contains no Argon2 implementation. | Pin an approved library and checked startup parameter floor/ceiling; add dependency and operational-cost tests. |
| A-10 | The initial dedicated-origin and no-forwarded-trust posture is decided, but production hostname, HTTPS/HSTS termination, and deployment wiring are not provisioned or represented in operational evidence. | Add environment examples and deployment contract before any production-shaped auth test is accepted. |
| A-11 | No password provisioning/rotation operator command exists, and no checked-in password-length/counting rule is shared with login validation. | Add one guarded operator path; reject equality with every configured secret and invalidate all sessions transactionally. |
| A-12 | Independent Gate A review has not occurred. | A reviewer must verify every row and finding against current code, then record pass or required changes. |

No code in Stages 0–1 is treated as security enforcement. The fixture review boundary is useful for
visual review but is not a substitute for server authorization.

## Authentication and runtime map

### Existing boundaries that Stage 2 must reuse

| Concern | Current code-grounded boundary | Stage 2 use |
|---|---|---|
| Private listener | `build_private_server()` creates `DishHTTPServer(..., surface_mode="private")`. | Frontend HTML, assets, schema, and API routes must dispatch only here. No third listener. |
| Action isolation | `DishRequestHandler.do_POST()` compares resolved surface with `surface_mode`; private/action mismatches return 404. | Extend the same explicit surface test to every frontend GET/POST route. |
| Process-wide admission | `DishHTTPServer.get_request()` and `admit_request()` share `_stop_event` and `_admission_lock`. | Frontend requests cross this gate before parsing or expensive password work. |
| Graceful drain | `_shutdown_servers()` calls `stop_accepting()` on both listeners, then shuts down and joins non-daemon handler threads. | Frontend handlers remain within this lifecycle; no framework-owned shutdown path. |
| Restore exclusion | `MaintenanceGate.request()` and `.restore()` serialize ordinary requests against database replacement. | Session/task-data database work must enter the same request gate, while the restore-safe security fence remains independently current. |
| Existing auth scope | `authenticate_bearer()` performs constant-time comparison for agent/admin/Action bearer tokens. | Remains unchanged and separate. Frontend cookies must never be accepted by this function. |
| Existing request limit | `DishRequestHandler._read_json()` bounds Content-Length and rejects malformed JSON/duplicates. | Reuse concepts, not the existing command envelope. Frontend schemas need their own smaller route-specific bounds and errors. |
| Existing service readiness | `_run_configured_service()` validates configuration and calls `startup_check()` before listener construction. | Frontend origin, Argon2 parameters, security material, persistence, and restore fence join startup readiness. |

### New server ownership proposed for Stage 2

The following paths are proposed implementation boundaries, not existing capabilities:

| Proposed module | Sole responsibility |
|---|---|
| `dish_service/frontend_http.py` | Private-listener route recognition, frontend request/response framing, static/HTML delivery, security headers, and API dispatch. |
| `dish_service/frontend_admission.py` | Canonical authority, Host, Origin, fetch metadata, singleton security headers/cookies, contract version, media type, and route body bounds before expensive work. |
| `dish_service/frontend_auth.py` | Password verification orchestration, session create/bootstrap/logout, CSRF verification, final response-release validation, and frontend principal creation. |
| `dish_service/frontend_security.py` | Token generation/verifiers, Argon2 parameter validation, CSRF derivation, security-generation checks, and timing-safe comparisons. |
| `dish_pg/frontend_security_models.py` | Frontend-only sessions, security generation, login limiter state, and security audit records. No task/workflow authority. |
| `dish_pg/frontend_security_repository.py` | Transactional persistence and cleanup for those support records. |
| `dish_tool/frontend_password_admin.py` | Guarded provisioning/rotation using the same counting rule and secret-distinctness validator as login configuration. |
| `frontend/src/js/features/auth/` | Concealed startup, bootstrap, fixed local expiry, login/logout, replacement fencing, page restore, suspension return, and cross-tab lifecycle signalling. |

These modules must not import or reproduce workflow policy. Protected task-data handlers receive a
validated frontend session principal and call read-only frontend query services only.

### Required persistence outcomes

The target schema names remain implementation-local, but the data model must establish these facts:

1. **Security generation:** a current monotonic frontend security generation or equivalent, changed by
   password/session-security rotation and checked during session creation and validation.
2. **Session verifier:** an opaque 256-bit-or-stronger browser token represented server-side only by a
   non-recoverable verifier, with fixed issue/expiry, revocation, security generation, and bounded
   metadata.
3. **Restore fence:** a current value not made older solely by PostgreSQL restore/PITR. A restored
   session is invalid until it agrees with this current fence.
4. **Limiter buckets/events:** peer and global failure windows durable across ordinary restart, with
   pre-verification admission and durable outcome update.
5. **Security audit:** successful and failed login, throttling, logout, expiry, rotation, and global
   invalidation, committed atomically with the security outcome where required.

A recommended implementation is a deployment-owned restore epoch stored outside restored PostgreSQL
and incorporated into the current frontend security generation. That recommendation is not accepted
until the database/deployment reviewer confirms backup, restore, permissions, atomic update, and
failure behavior.

### Browser storage and coordination ownership

- The session token exists only in the `__Host-dish_frontend_session` HttpOnly cookie.
- The CSRF proof and session bootstrap object remain in memory.
- Task data remains in memory and DOM only; no localStorage, IndexedDB, Cache API, service worker, or
  persistent client cache.
- `BroadcastChannel` is the preferred ephemeral same-origin lifecycle signal; the signal contains only
  a generation/event nonce and lifecycle kind, never task or credential data.
- Local expiry is the earlier safe bound derived from the absolute expiry and request-start-relative
  remaining seconds. Activity never changes it.
- Logout immediately conceals the current tab before network work. Other active tabs conceal within
  two seconds; resumed contexts validate before reveal.

## OpenAPI ownership map

| Artifact | Current state | Required owner/action |
|---|---|---|
| Action schema | Generated by `dish_service/openapi.py` and exposed at `/openapi/action.json`. | Unchanged; existing synchronization tests remain authoritative. |
| Frontend schema source | `frontend/openapi/frontend.openapi.json` exists and defines six bounded operations. | Frontend owns it; Stage 2 must reconcile it with the full normative auth/error contract before serving it. |
| Frontend generated client | `frontend/src/js/api/generated/frontend-client.js`, generated by `frontend/tools/generate-client.mjs`. | Continue deterministic generation; add service/schema synchronization tests. |
| Private schema route | Absent. | Serve only on private listener and only after live frontend-session validation. |
| Default docs/schema | No framework route exists today. | Keep disabled if a framework is introduced. |
| Contract version | Browser constant and schema use `dish-frontend-v1`. | Server selects exactly one supported value and emits it on every frontend API response. |

## Error, notice, DTO, storage, accessibility, and deployment owners

| Contract family | Implementation owner | Test owner |
|---|---|---|
| Login/session/logout errors | Frontend admission/auth service and closed frontend OpenAPI schemas. | Unit parser tests, PostgreSQL integration, HTTP integration, Playwright lifecycle suite. |
| Board/detail errors | Later frontend query/application services; browser response validator. | DTO schema tests, service integration, browser last-safe-view tests. |
| Notices and banners | Backend notice registry plus browser notice presentation. | Registry equivalence/unit tests and browser announcement/grouping tests. |
| Session DTO | Frontend auth service; generated browser validator/client. | Clock/fence unit tests, HTTP contract tests, restart/restore integration, browser expiry tests. |
| Board/detail DTOs | Frontend read projection/application services. | Gate B mapping tests, query integration, browser contract mismatch tests. |
| Browser storage | `frontend/src/js/features/auth/` and refresh state modules. | Storage inspection in Playwright; no persistent protected data. |
| Accessibility | Existing feature modules plus real lifecycle controls. | Unit semantics, keyboard/focus browser tests, live-region inspection. |
| Deployment | Service config, systemd env examples, private HTTPS proxy/host configuration. | Startup validation, listener isolation, header tests, production-shaped acceptance. |

## Requirement traceability

“Planned” means there is a named implementation/test owner but no integrated code yet. “Fixture” is
implemented only for design review. “Future product” is intentionally outside product Stage 1.

| Contract section | Delivery owner | Implementation boundary | Required evidence | Readiness |
|---|---|---|---|---|
| `frontend.md` 1 Purpose | Stages 0–7 | Whole frontend slice | Final Stage 1 acceptance | Planned |
| 2.1 Private read-only board | Stages 2–7 | Auth, board, detail, refresh | Service and browser acceptance | Fixture only |
| 2.2 Structured editing | Future product | None in Stage 1 | Scope exclusion tests | Ready/excluded |
| 2.3 Cooking planner | Future product | None in Stage 1 | Scope exclusion tests | Ready/excluded |
| 3.1 Board | Stages 1, 3, 5 | Board feature and query service | Visual, query, refresh, browser tests | Fixture only |
| 3.2 Task cards | Stages 1, 3, 5 | Card DTO/presentation | DTO and browser tests | Fixture only |
| 3.3 Task side panel | Stages 1, 4, 5 | Detail query, renderer, panel | Rendering/service/browser tests | Fixture only |
| 3.4 Refresh and continuity | Stage 5 | Reconciliation feature/service | Race and continuity browser tests | Planned |
| 3.5 Warnings and errors | Stages 1, 5 | Notice registry and response validator | Registry, lifecycle, browser tests | Fixture only |
| 3.6 Login and session | Stage 2 | Frontend auth boundaries above | Security/integration/browser suite | Blocked A-01–A-11 |
| 3.7 Device profile | Stages 1, 6, 7 | CSS/layout/accessibility | 1024+ browser matrix | Fixture passed |
| 4.1 Canonical authority | Stages 3–5 | PostgreSQL query services | Gate B and equivalence tests | Gate B pending |
| 4.2 Factual summaries | Stages 3–4 | Backend DTO builders | Predicate and schema tests | Gate B pending |
| 4.3 Next-step guidance | Stage 4 | Backend factual advisory service | Workflow-fact equivalence | Gate B pending |
| 4.4 Aliases/projection | Stages 3–5 | Projection read projection | Query/integration tests | Gate B pending |
| 4.5 Disclosure | Stage 4 | Versioned disclosure registry | Registry and detail contract tests | Gate B pending |
| 5 Scope exclusions | All stages | Route/schema negative surface | HTTP and browser negative tests | Planned |
| 6 Stage 1 acceptance | Stage 7 | Production-shaped full slice | Full acceptance record | Planned |
| 6.1 Staged delivery | Every stage | Stage notes, screenshots, Gate C | Human review record | Stages 0–1 complete |
| 7 Cross-stage invariants | Every stage | Authority and lifecycle boundaries | Architecture/service/browser tests | Planned |
| 8 Backend authority | Stages 3–5 | Read-only services over PostgreSQL | Gate B and no-policy-duplication review | Pending |
| 9 Provenance | Documentation | Current contracts | Review | Ready |
| `frontend-imp.md` 1 Ownership | All integrated stages | Frontend-owned vertical slice | Architecture review | Planned |
| 2 Listener/admission/trust | Stage 2 | Existing `DishHTTPServer` plus frontend admission | Isolation, drain, origin tests | Blocked A-01/A-02 |
| 3 OpenAPI/routes | Stages 0, 2–4 | Separate frontend schema/client/routes | Sync and HTTP contract tests | Partial; A-07 |
| 4 Authentication/session | Stage 2 | Auth/security/persistence/browser modules | Full security matrix | Blocked A-03–A-11 |
| 5.1 Board/pagination | Stage 3 | Board query service/read model | Gate B, plans, performance | Pending Gate B |
| 5.2 Task detail | Stage 4 | Detail query/DTO service | Gate B, coherence tests | Pending Gate B |
| 5.3 Rendering | Stage 4 | Pinned backend renderer/sanitizer | Security corpus/browser tests | Planned |
| 5.4 History exclusion | All | No history route/DTO | Negative route/schema tests | Ready/excluded |
| 6 Refresh/reconciliation | Stage 5 | Refresh coordinator/browser fencing | Race, movement, stale response tests | Planned |
| 7 Aliases/projection | Stages 3–5 | PostgreSQL projection reader | Gate B and abnormal-state tests | Pending Gate B |
| 8 Errors/banners | Stages 2–5 | Error and notice registries | Registry/HTTP/browser tests | Partial fixture |
| 9 Deep links/state | Stages 4–5 | Router and protected restoration | Browser navigation/session tests | Fixture only |
| 10 Accessibility | Stages 1, 2, 6, 7 | Feature modules and semantic DOM | Keyboard/focus/live region suite | Partial fixture |
| 11 Staging/modularity | Every stage | Existing module/file-size checks | Normal repository gates | Ready for current frontend |
| 11.1 Organization | Every stage | Existing frontend structure | Lint/source-size tests | Passed for Stages 0–1 |
| 11.2 Gates | Gates A/B | This packet and Gate B map | Independent reviews | Pending |
| 11.3–11.4 Stages 0–1 | Complete | Fixture frontend | Check/browser/screenshots | Complete |
| 11.5 Stage 2 | Stage 2 | Auth/protected shell | Gate A plus stage evidence | Blocked |
| 11.6 Stage 3 | Stage 3 | Real board | Gate B plus stage evidence | Blocked |
| 11.7 Stage 4 | Stage 4 | Real detail | Extended Gate B | Blocked |
| 11.8 Stage 5 | Stage 5 | Refresh/continuity | Integrated race evidence | Planned |
| 11.9 Stage 6 | Stage 6 | Hardening/deployment | Production-shaped checks | Planned |
| 11.10 Stage 7 | Stage 7 | Committed Playwright suite | Full green run | Planned |
| 12 Acceptance families | Stages 2–7 | Named owners above | Acceptance checklist | Planned |
| 13 Deployment/operations | Stages 2, 6 | Config/systemd/private HTTPS origin | Startup/deployment/header tests | Blocked A-10 |

## Gate A completion checklist

Gate A may be marked passed only when all of the following are true:

- A reviewer has inspected the complete current contracts and current code, not only this summary.
- A-01 through A-11 are resolved in code/config/schema or the contracts are explicitly amended.
- The target PostgreSQL migration includes accepted frontend support-state migrations.
- Restore/PITR evidence proves an old database cannot make an old frontend session valid.
- Login limiter persistence and failure-closed behavior are tested across restart.
- Password provisioning and rotation use the same exact counting/bounds rules as login.
- Existing bearer-token routes and Action OpenAPI synchronization remain unchanged.
- Private/action route-isolation, graceful drain, origin validation, duplicate security headers/cookies,
  response headers, and cache behavior have tests.
- The reviewer records an exact commit/build and a clear pass decision in the review record.

## Prepared implementation and test handoff

The blocked implementation sequence is recorded in `frontend-stage2-implementation-checklist.md`.
Executable acceptance ownership is predeclared in
`../frontend/contracts/stage2-acceptance-cases.json`; test deployment entry and rollback are recorded
in `frontend-test-deployment-readiness.md`. These artifacts reduce Stage 2 startup work but
are not implementation evidence and do not alter the Gate A pass conditions.
