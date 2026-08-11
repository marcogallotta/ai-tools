# Frontend Gate A readiness packet

## Status

**Authoring review complete; Gate A is not passed.**

This packet was prepared against the current `frontend.md`, `frontend-imp.md`, service runtime,
PostgreSQL model set, deployment examples, frontend OpenAPI document, and current production/review
frontend builds. Gate A still requires an independent reviewer who did not author the implementation.
It also has the
material acceptance/deployment findings listed below. A Stage 2 implementation candidate is now checked in,
but it is not accepted for production exposure until those findings are resolved and the reviewer records
acceptance in `frontend-gate-a-review.md`.

Refreshed against the repository state containing implementation candidates through Delivery Stage 7
and checked-in Alembic head `0037_release_identity_contract`. The frontend-specific support tables were
introduced by revision `0033_frontend_security`; the machine-readable security decisions remain in
`../frontend/contracts/stage2-security-contract.json`. This packet does not assume that the current
head has deployed acceptance or that PostgreSQL is production task/workflow authority.

## Evidence inspected

- Product and implementation contracts: `docs/frontend.md`, `docs/frontend-imp.md`.
- Runtime authority map: `docs/architecture/index.md`, `docs/runtime-contract.md`.
- Listener and drain path: `dish_service/http.py`, `dish_service/__main__.py`.
- Existing service configuration and bearer authentication: `dish_service/config.py`,
  `dish_service/auth.py`.
- Existing request/restore gates: `dish_service/maintenance.py`,
  `dish_service/request_coordinators.py`, and restore modules under `dish_service/`.
- Current PostgreSQL authority models and read surface: `dish_pg/models.py`,
  `dish_pg/stage3_models.py`, `dish_pg/stage5_models.py`, `dish_pg/read_model.py`.
- Deployment configuration: `deploy/systemd/service.env.example` and service unit examples.
- Frontend contract/build: `frontend/openapi/frontend.openapi.json`, `frontend/src/`,
  `frontend/tools/`, `frontend/tests/`, and the Stage 7 acceptance command in `testing.md`.
- Current private frontend deployment units and Caddy configuration under `deploy/systemd/` and
  `deploy/caddy/`.
- Python dependency pins: `requirements.txt`, `requirements-test.txt`.

## Material findings blocking Gate A

| ID | Finding | Required resolution before Stage 2 acceptance/exposure |
|---|---|---|
| A-01 | Validated canonical-origin configuration, no-forwarded-trust admission, bounded singleton/header/body parsing, and request ordering now have an implementation candidate. | Complete independent review and production-shaped admission evidence. |
| A-02 | Private-listener frontend routing/static delivery/response security headers now have an implementation candidate; Action-listener isolation has focused HTTP coverage. | Complete regression/production-shaped isolation and response-header evidence. |
| A-03 | The Argon2id shared-password/session/CSRF application boundary now exists as an implementation candidate and remains separate from bearer-token auth. | Complete native PostgreSQL, concurrency, restart, and independent security review evidence. |
| A-04 | `0033_frontend_security` adds frontend-only security state, sessions, durable login events, and security audit persistence within the current `0037_release_identity_contract` chain. | Certify the current migration chain and lifecycle against native PostgreSQL and accept the support-state shape. |
| A-05 | An owner-only external restore-fence file is now bound by hash into PostgreSQL security state and session validation fails closed on mismatch. | Prove destructive restore/PITR cannot revive session authority; independently review permissions/update/failure behavior. |
| A-06 | A dedicated frontend parser/response writer now implements the closed request/error/cookie/header contract. | Complete ambiguity, malformed-input, cache, and production-shaped transport review. |
| A-07 | The private authenticated runtime now serves the bounded frontend OpenAPI document and the generated client remains independently synchronized; Action OpenAPI remains unchanged. | Complete served-schema/runtime synchronization and isolation review. |
| A-08 | Browser bootstrap, fixed-expiry concealment, logout retry, page-restore/suspension revalidation, opaque returns, ephemeral cross-tab signalling, and a committed Stage 7 browser suite now exist. | Run and accept the complete suite against the exact deployed HTTPS build, including multi-tab and lifecycle evidence. |
| A-09 | `argon2-cffi` is pinned and startup validates an explicitly configured Argon2id policy without inventing production values. | Independently review/approve production time, memory, parallelism, hash, and salt parameters and operational cost. |
| A-10 | Runtime configuration is fail-closed and environment examples keep frontend auth disabled by default. | Provision and verify the dedicated HTTPS hostname, HSTS termination, canonical origin, owner-only secrets/fence, and production deployment wiring. |
| A-11 | `scripts/dish-frontend-security` now provides guarded fence initialization, password provisioning/rotation, and restore-fence rotation using the shared password bounds and global session invalidation. | Complete native/operator review and recovery/runbook evidence. |
| A-12 | Independent Gate A review has not occurred. | A reviewer must verify every row and finding against current code, then record pass or required changes. |

No code in Stages 0–1 is treated as security enforcement. The new Stage 2 candidate is the security
enforcement path, but its fixture mode and implementation status are not substitutes for Gate A acceptance
or production HTTPS/deployment evidence.

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

### Stage 2 server ownership candidate

The following paths are now checked-in implementation boundaries pending Gate A acceptance:

| Candidate module | Sole responsibility |
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
| Frontend schema source | `frontend/openapi/frontend.openapi.json` defines seven bounded operations. | Keep the served runtime and generated client synchronized with the normative auth/error contract. |
| Frontend generated client | `frontend/src/js/api/generated/frontend-client.js`, generated by `frontend/tools/generate-client.mjs`. | Deterministic generation and synchronization checks are present; retain them as acceptance evidence. |
| Private schema route | The candidate serves `/openapi/frontend.json` only through protected private dispatch. | Independently verify listener and live-session isolation against the exact build. |
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

“Candidate” means integrated code exists but the applicable independent or deployed evidence remains
pending. “Fixture” is design-review-only. “Future product” is outside product Stage 1.

| Contract section | Delivery owner | Implementation boundary | Required evidence | Readiness |
|---|---|---|---|---|
| `frontend.md` 1 Purpose | Stages 0–7 | Whole frontend slice | Final Stage 1 acceptance | Candidate implemented; acceptance pending |
| 2.1 Private read-only board | Stages 2–7 | Auth, board, detail, refresh | Service and browser acceptance | Candidate implemented behind private/local boundaries |
| 2.2 Structured editing | Future product | None in Stage 1 | Scope exclusion tests | Ready/excluded |
| 2.3 Cooking planner | Future product | None in Stage 1 | Scope exclusion tests | Ready/excluded |
| 3.1 Board | Stages 1, 3, 5 | Board feature and query service | Visual, query, refresh, browser tests | Candidate implemented |
| 3.2 Task cards | Stages 1, 3, 5 | Card DTO/presentation | DTO and browser tests | Candidate implemented |
| 3.3 Task side panel | Stages 1, 4, 5 | Detail query, renderer, panel | Rendering/service/browser tests | Candidate implemented |
| 3.4 Refresh and continuity | Stage 5 | Reconciliation feature/service | Race and continuity browser tests | Candidate implemented |
| 3.5 Warnings and errors | Stages 1, 5 | Notice registry and response validator | Registry, lifecycle, browser tests | Candidate implemented |
| 3.6 Login and session | Stage 2 | Frontend auth boundaries above | Security/integration/browser suite | Blocked A-01–A-11 |
| 3.7 Device profile | Stages 1, 6, 7 | CSS/layout/accessibility | 1024+ browser matrix | Automated candidate evidence present |
| 4.1 Canonical authority | Stages 3–5 | PostgreSQL query services | Gate B and equivalence tests | Gate B pending |
| 4.2 Factual summaries | Stages 3–4 | Backend DTO builders | Predicate and schema tests | Gate B pending |
| 4.3 Next-step guidance | Stage 4 | Backend factual advisory service | Workflow-fact equivalence | Gate B pending |
| 4.4 Aliases/projection | Stages 3–5 | Projection read projection | Query/integration tests | Gate B pending |
| 4.5 Disclosure | Stage 4 | Versioned disclosure registry | Registry and detail contract tests | Gate B pending |
| 5 Scope exclusions | All stages | Route/schema negative surface | HTTP and browser negative tests | Candidate tests present |
| 6 Stage 1 acceptance | Stage 7 | Production-shaped full slice | Full acceptance record | Suite committed; exact deployment acceptance pending |
| 6.1 Staged delivery | Every stage | Stage notes, screenshots, Gate C | Human review record | Implementation through Stage 7 present; sign-offs pending |
| 7 Cross-stage invariants | Every stage | Authority and lifecycle boundaries | Architecture/service/browser tests | Candidate evidence present |
| 8 Backend authority | Stages 3–5 | Read-only services over PostgreSQL | Gate B and no-policy-duplication review | Pending |
| 9 Provenance | Documentation | Current contracts | Review | Ready |
| `frontend-imp.md` 1 Ownership | All integrated stages | Frontend-owned vertical slice | Architecture review | Implemented candidate |
| 2 Listener/admission/trust | Stage 2 | Existing `DishHTTPServer` plus frontend admission | Isolation, drain, origin tests | Blocked A-01/A-02 |
| 3 OpenAPI/routes | Stages 0, 2–4 | Separate frontend schema/client/routes | Sync and HTTP contract tests | Candidate present; A-07 review pending |
| 4 Authentication/session | Stage 2 | Auth/security/persistence/browser modules | Full security matrix | Blocked A-03–A-11 |
| 5.1 Board/pagination | Stage 3 | Board query service/read model | Gate B, plans, performance | Pending Gate B |
| 5.2 Task detail | Stage 4 | Detail query/DTO service | Gate B, coherence tests | Pending Gate B |
| 5.3 Rendering | Stage 4 | Pinned backend renderer/sanitizer | Security corpus/browser tests | Candidate implemented |
| 5.4 History exclusion | All | No history route/DTO | Negative route/schema tests | Ready/excluded |
| 6 Refresh/reconciliation | Stage 5 | Refresh coordinator/browser fencing | Race, movement, stale response tests | Candidate implemented |
| 7 Aliases/projection | Stages 3–5 | PostgreSQL projection reader | Gate B and abnormal-state tests | Pending Gate B |
| 8 Errors/banners | Stages 2–5 | Error and notice registries | Registry/HTTP/browser tests | Candidate implemented |
| 9 Deep links/state | Stages 4–5 | Router and protected restoration | Browser navigation/session tests | Candidate implemented |
| 10 Accessibility | Stages 1, 2, 6, 7 | Feature modules and semantic DOM | Keyboard/focus/live region suite | Candidate implemented |
| 11 Staging/modularity | Every stage | Existing module/file-size checks | Normal repository gates | Ready for current frontend |
| 11.1 Organization | Every stage | Existing frontend structure | Lint/source-size tests | Current checks present |
| 11.2 Gates | Gates A/B | This packet and Gate B map | Independent reviews | Pending |
| 11.3–11.4 Stages 0–1 | Complete | Fixture frontend | Check/browser/screenshots | Complete |
| 11.5 Stage 2 | Stage 2 | Auth/protected shell | Gate A plus stage evidence | Candidate implemented; activation blocked |
| 11.6 Stage 3 | Stage 3 | Real board | Gate B plus stage evidence | Candidate implemented; activation blocked |
| 11.7 Stage 4 | Stage 4 | Real detail | Extended Gate B | Candidate implemented; activation blocked |
| 11.8 Stage 5 | Stage 5 | Refresh/continuity | Integrated race evidence | Candidate implemented |
| 11.9 Stage 6 | Stage 6 | Hardening/deployment | Production-shaped checks | Candidate implemented |
| 11.10 Stage 7 | Stage 7 | Committed Playwright suite | Full green run | Suite committed; deployment run pending |
| 12 Acceptance families | Stages 2–7 | Named owners above | Acceptance checklist | Candidate evidence present; final acceptance pending |
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

## Acceptance and deployment handoff

Executable acceptance ownership is declared in
`../frontend/contracts/stage2-acceptance-cases.json`. Stage 7 browser execution is maintained in
`testing.md`, and environment provisioning, probes, and rollback are maintained in
`frontend-deployment-runbook.md`. None of those artifacts alters the Gate A pass conditions.
