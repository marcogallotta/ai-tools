# Frontend Stage 2 implementation checklist

Status: prepared; execution blocked until Gate A passes.

This is the commit-planning checklist for real authentication. It is not an authorization to begin
Stage 2 and contains no substitute persistence path.

## 2A — configuration, admission, and private route framing

- add validated canonical-origin configuration using the closed Stage 2 runtime decisions;
- add private-listener frontend route recognition and Action-listener 404 tests;
- add route-specific request bounds, singleton-header/cookie parsing, media and contract validation;
- add frontend response framing, closed errors, security headers, cache policy, and static/HTML delivery;
- serve the protected frontend OpenAPI document and prove Action schema isolation.

Exit evidence: `S2-HTTP-*`, `S2-SECURITY-001`, and `S2-OPENAPI-001` acceptance cases are executable
and green without session creation yet being exposed as usable authority.

## 2B — security persistence and restore fence

- land accepted frontend security migrations and repository transactions;
- provision the independently current restore/PITR fence and bind sessions to it;
- implement limiter read/update, security generation, sessions, audit, cleanup, and startup checks;
- prove restart persistence, restore invalidation, transaction rollback, and bounded cleanup.

Exit evidence: `S2-AUTH-002`, `S2-AUTH-003`, and `S2-SESSION-003` are green against real PostgreSQL.

## 2C — password administration and authentication service

- pin Argon2id and accepted parameter floors/ceilings;
- implement the shared Unicode-code-point validator and secret-distinctness checks;
- add guarded provisioning/rotation with transactional global invalidation;
- implement login verification, replacement, session creation, bootstrap, logout, and final response
  release validation.

Exit evidence: all `S2-AUTH-*`, `S2-SESSION-*`, `S2-LOGOUT-*`, and `S2-PASSWORD-*` cases are green.

## 2D — browser lifecycle and protected fixture shell

- replace the fixture login transition with the generated API client;
- implement concealed bootstrap, safe fixed local expiry, login replacement fences, and logout retry;
- add memory-only CSRF state, cross-tab signalling, page-restore/suspension revalidation, and opaque
  deep-link return handling;
- retain clearly marked fixture board content until Stage 3.

Exit evidence: `S2-BROWSER-*` and the complete Stage 2 Playwright matrix are green.

## Stage 2 handoff record

Record exact migration head, build, Argon2 parameters, dedicated origin, restore-fence identity,
accepted Gate A review, tests run, and any reopened dependency. Do not mark Stage 2 complete while a
fixture transition can bypass server authentication.
