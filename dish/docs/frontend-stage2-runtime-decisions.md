# Frontend Stage 2 runtime decisions

Status: implementation candidate present; Gate A remains unpassed pending acceptance/deployment evidence.

This document records the pre-database engineering choices that were left implementation-local by
`frontend-imp.md`. The machine-readable counterpart is
`../frontend/contracts/stage2-security-contract.json`. The checked-in implementation now follows these decisions, but that implementation does not pass Gate A
and does not waive native PostgreSQL, destructive-restore, cryptographic-parameter, browser-matrix, deployment,
or independent-review requirements.

## Route and listener ownership

The existing private `DishHTTPServer` owns every frontend route. No third listener or framework-owned
shutdown lifecycle is permitted.

The initial route grammar is:

- unauthenticated: `GET /login`, versioned/public files below `GET /assets/`, `GET /styles/`, and
  `GET /js/`, plus `POST /frontend/login`;
- protected HTML: `GET /`, canonical Stage 4 `GET /dishes/{stored_dish_uuid}/{decorative_slug}`, and the
  authenticated fixture-only `GET /task/{fixture_id}` review path;
- protected API: the six operations in `frontend/openapi/frontend.openapi.json` plus
  `GET /openapi/frontend.json`;
- lifecycle exception: `POST /frontend/logout`, which may only revoke or confirm cleanup for the
  presented session and may never return protected data.

The Action listener returns 404 for all of those paths. The existing Action schema route and bearer
surfaces remain unchanged.

## Canonical origin and proxy posture

`DISH_FRONTEND_ORIGIN` will contain one absolute HTTPS origin with no path, query, fragment, userinfo,
or wildcard. Its hostname is dedicated to Dish and differs from the Action/Funnel hostname. Port
separation alone does not satisfy this requirement.

The initial implementation has no trusted forwarded-authority or forwarded-client-address mode.
`Forwarded`, `X-Forwarded-Host`, `X-Forwarded-Proto`, and client-address headers are ignored. The
private listener remains loopback-only; the configured origin and request `Host` establish browser
authority, while the direct socket peer is the only peer-rate-limit identity. This is deliberately
conservative for a single-user private deployment and avoids converting unauthenticated proxy headers
into authority.

A later authenticated-proxy design requires a separate reviewed contract and cannot be enabled by a
loose environment toggle.

State-changing browser requests require exactly one canonical `Origin` and same-origin fetch metadata.
CORS is disabled.

## Request ambiguity and ordering

Admission rejects duplicate or conflicting singleton security values before password hashing,
session lookup, or application dispatch. This includes `Host`, `Origin`,
`X-Dish-Frontend-Contract`, `X-Dish-CSRF`, and duplicate frontend-session cookie occurrences.

The processing order is:

1. process-wide listener admission and drain gate;
2. request-line, header-count, header-size, and whole-body bounds;
3. private-listener route recognition;
4. canonical Host/origin, fetch metadata, media type, contract version, and singleton validation;
5. route-specific JSON framing and closed-schema validation;
6. login limiter decision or session/lifecycle validation;
7. expensive Argon2 work or protected application/database work;
8. final session validity check before releasing protected data.

## Password contract

Provisioning, rotation, OpenAPI validation, and login use Unicode code-point count without trimming,
normalization, or case folding. The accepted range is 16 through 1024 code points; the login schema
continues to allow short strings so incorrect submissions receive the generic login outcome rather
than exposing password policy.

Only an Argon2id verifier is stored. Provisioning rejects equality with every configured agent,
admin, Action, session/CSRF, route-identity, or other security secret. Argon2 floors, ceilings, and
operational tuning remain security-review items and are not guessed here.

## Cookie, CSRF, and lifecycle contract

The sole browser credential is `__Host-dish_frontend_session`, set with `Secure`, `HttpOnly`,
`SameSite=Strict`, `Path=/`, and no `Domain`. It has a fixed non-sliding 604800-second lifetime. Login
replacement is one committed outcome and stale lifecycle responses cannot affect a newer session.

Session bootstrap returns only `expires_at`, `remaining_seconds`, and `csrf_proof`. The proof is
header-safe, has at least 128 bits of effective forgery resistance, remains memory-only in the
browser, and is required only for logout in Stage 2. Protected task/view data is memory-only; no
service worker is registered.

The browser calculates the local concealment deadline from both the absolute expiry and the
request-start-relative remaining duration, taking the earlier safe boundary. Page restoration and
return from suspension revalidate before revealing protected content. Logout conceals immediately,
propagates to active same-origin tabs within two seconds, and remains concealed with explicit retry on
an unresolved network/server outcome.

## Response and cache policy

Every frontend response sends `X-Content-Type-Options: nosniff`,
`Cross-Origin-Resource-Policy: same-origin`, and `Referrer-Policy: no-referrer`. HTML also sends
`Cross-Origin-Opener-Policy: same-origin`, the restrictive permissions policy and CSP recorded in the
machine-readable contract.

Login responses, protected HTML, protected APIs, bootstrap, and logout are `Cache-Control: no-store`.
Only versioned public assets may be immutable. HSTS is owned by the trusted HTTPS termination point,
not duplicated as proof that plaintext service access is safe.

## What remains blocked

The checked-in candidate now includes the PostgreSQL support migration, persistent limiter/session/audit
records, external restore-fence binding, Argon2id dependency and verifier, guarded password administration,
private-listener admission/response handling, and browser lifecycle implementation. The following remain
acceptance/deployment blockers:

- native PostgreSQL migration/authentication evidence and restart/transaction evidence;
- destructive restore/PITR evidence proving the independently current fence prevents revival;
- independently accepted production Argon2 parameters;
- the complete browser concurrency/lifecycle matrix, including real HTTPS cookie behavior;
- actual dedicated-hostname/TLS/HSTS deployment;
- independent Gate A review.

`DISH_FRONTEND_ENABLED` remains disabled by default. Enabling authentication does not automatically enable
Stage 3/4 PostgreSQL observation reads; those require the separate
`DISH_FRONTEND_POSTGRESQL_READS_ENABLED` activation and its still-gated read-surface configuration. During dark launch the private runtime keeps those concerns on **physically distinct PostgreSQL databases**:
`DISH_FRONTEND_DATABASE_URL` is the separate writable frontend-security/session/throttle/audit database used by
password administration, while `DISH_FRONTEND_OBSERVATION_DATABASE_URL` is the dark-launch database required
only for enabled Stage 3/4 reads and is intended for SELECT-only credentials. Startup compares server-reported
database identity and fails closed when both connections resolve to the same database, regardless of different
roles, aliases, or read-only connection options. Board/detail transactions remain explicitly read-only; this
split does not transfer task authority to PostgreSQL.
