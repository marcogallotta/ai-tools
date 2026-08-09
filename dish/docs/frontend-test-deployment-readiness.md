# Frontend test deployment readiness

Status: Stage 2 implementation candidate present; test deployment remains blocked on Gate A evidence.

This checklist owns the first test-environment deployment after Gate A acceptance. The checked-in
`0033_frontend_security` migration and authentication candidate provide the implementation surface, but
this checklist does not authorize production exposure or treat unreviewed code as accepted security.

## Required topology

- reuse the test service's existing private listener and process lifecycle;
- expose one dedicated tailnet-only HTTPS hostname for the browser/API origin;
- keep the Action/Funnel hostname separate; a different port on the same hostname is insufficient;
- keep the service listener loopback-only and do not introduce a third listener;
- use no forwarded Host, scheme, or client-address authority in the initial implementation;
- terminate HSTS at the trusted HTTPS edge and expose no plaintext credential endpoint.

The current `tailscale serve` examples for CLI/admin ports are not the frontend deployment topology.
A concrete dedicated hostname/certificate mechanism must be rehearsed and recorded before Gate A can
pass.

## Configuration inventory

The Stage 2 implementation candidate requires validated configuration for:

- canonical `DISH_FRONTEND_ORIGIN`;
- Argon2id verifier plus approved memory, time, and parallelism limits;
- frontend session/CSRF and route-identity security material, pairwise distinct from all existing
  service tokens and the password;
- fixed session lifetime of 604800 seconds;
- peer/global limiter windows and ceilings fixed by the contract;
- independently current restore-fence location/identity;
- PostgreSQL connection and frontend support migration head;
- bounded frontend request, response, section, page, cursor, and rendering limits.

Do not put the plaintext password, session token, CSRF proof, or task data in environment examples,
systemd status, command lines, logs, HTML boot data, or screenshots.

## Test deployment entry gate

- [ ] Gate A independent review records PASS against an exact build and schema revision.
- [ ] Frontend security migrations are applied to the test PostgreSQL database.
- [ ] The restore/PITR fence is provisioned outside the restorable database and startup validates it.
- [ ] The shared password is provisioned through the guarded operator path.
- [ ] Dedicated hostname, HTTPS, HSTS, and origin configuration are active.
- [ ] Existing private CLI/admin and Action routes pass regression and isolation tests.
- [ ] Frontend OpenAPI is served only after a live frontend session.
- [ ] Fixture review mode is disabled in the deployed authenticated build.

## Production-shaped probes

Run and record:

1. unauthenticated login shell and versioned assets load; every other frontend HTML/API/schema route
   remains concealed or rejects appropriately;
2. the Action listener returns 404 for all frontend routes and frontend schema paths;
3. wrong Host, Origin, fetch metadata, contract version, media type, duplicate singleton headers, and
   duplicate session cookies fail before Argon2/session/domain work;
4. login, replacement, bootstrap, restart persistence, fixed expiry, logout, lost responses, and
   multi-tab clearing match the Stage 2 acceptance manifest;
5. cache, cookie, CSP, CORP, COOP, referrer, permissions, redirect, and no-service-worker behavior is
   inspected from the real HTTPS origin;
6. ordinary restart preserves valid sessions, while destructive restore/PITR invalidates restored
   session authority;
7. access logs, application logs, journals, metrics, and failure responses contain no password,
   verifier, session/cursor token, CSRF proof, task title/body, or raw canonical identity;
8. graceful drain stops new admission and completes or safely withholds in-flight protected responses.

## Rollback and cleanup

A failed test deployment must be able to:

- remove the dedicated frontend HTTPS mapping without altering the Action route;
- disable frontend route dispatch while leaving existing private diagnosis/admin behavior available;
- revoke all frontend sessions by advancing the accepted security fence/generation;
- preserve security audit evidence without retaining plaintext credentials or protected task data;
- leave the PostgreSQL support migration in an understood forward-compatible state or apply its tested
  downgrade only when the migration contract permits it.

Production rollout requires a separate Stage 6/7 production-shaped record after real board/detail and
refresh behavior exist.
