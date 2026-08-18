# Frontend deployment runbook

Status: the authenticated frontend has a production-shaped PostgreSQL TEST path.
Production exposure remains disabled unless the exact production candidate is
approved in `frontend-activation.md`. TEST rehearsal never changes that record.

This runbook owns deployment and rollback. Product behavior remains in
`frontend.md`, durable boundaries remain in `architecture/`, and the production
activation decision remains in `frontend-activation.md`.

## Entry conditions

- The target is explicit. For PostgreSQL TEST, use the disposable TEST authority
  candidate and record its exact commit/build, Alembic head, Dish release, active
  authority generation, frontend contract, and configuration. TEST evidence is
  rehearsal evidence only and never authorizes production activation.
- For production, `frontend-activation.md` must record APPROVE for the exact
  commit, build, contract, schema, target, and configuration being deployed.
  Production administration still requires Marco's explicit authorization.

## Required topology

- Reuse the existing private listener and process lifecycle; do not add a third
  listener.
- Use a dedicated private HTTPS hostname distinct from the Action/Funnel
  hostname. Port separation is insufficient because browser cookies are not
  port-scoped.
- Keep the service listener loopback-only. The initial implementation ignores
  forwarded Host, scheme, and client-address headers.
- Terminate HSTS at the trusted HTTPS edge and expose no plaintext credential
  endpoint.
- Keep the writable frontend security database physically distinct from the
  PostgreSQL observation database. Use SELECT-only credentials for reads.

## Provisioning

1. Start from the environment-specific systemd example and populate every
   required frontend value. For PostgreSQL TEST, use
   `deploy/systemd/service-test.env.example` plus
   `deploy/systemd/frontend-test-caddy.env.example`. The frontend origin and
   Action origin must use different hostnames.
2. Apply the current Alembic head to the writable database named by
   `DISH_FRONTEND_DATABASE_URL`.
3. Provision the owner-only restore fence outside PostgreSQL:

   ```sh
   scripts/dish-frontend-security fence-init
   ```

4. Provision the shared password through the guarded prompt:

   ```sh
   scripts/dish-frontend-security provision
   ```

5. Build the production-shaped frontend; never deploy `review-dist/`:

   ```sh
   npm --prefix frontend run build
   ```

6. Configure the dedicated HTTPS mapping, then enable authentication. For TEST,
   install `deploy/systemd/dish-frontend-test-caddy.service` and
   `deploy/caddy/dish-frontend-test.Caddyfile`; it binds the configured private
   address and proxies only to the existing TEST private listener on
   `127.0.0.1:8765`. It must never proxy to the TEST Action listener on 8766.
   Enable PostgreSQL reads only for the exact candidate and reviewed
   projection-delay setting.
7. In PostgreSQL authority mode, startup independently validates the authority
   service identity and the SELECT-only frontend observation connection. The
   observation database, Alembic head, Dish release, and active generation must
   match exactly; auth/session storage must resolve to a different physical
   PostgreSQL database. Any mismatch fails startup closed.

Never place plaintext passwords, session or cursor tokens, CSRF proofs, task
data, or database credentials in command arguments, logs, screenshots,
environment examples, or HTML boot data.

## Production-shaped probes

Record the exact commit, build, migration head, schema fingerprint, frontend
contract version, target, and configuration identity. Verify:

1. login, assets, protected HTML/API/schema concealment, fixed expiry,
   replacement, logout, restart, and multi-tab clearing;
2. Action-listener 404 isolation for all frontend paths;
3. Host, Origin, fetch metadata, media type, contract version, singleton
   header/cookie, and body bounds reject before Argon2 or protected database
   work;
4. cookie, cache, CSP, CORP, COOP, referrer, permissions, redirect, and
   no-service-worker behavior from the real HTTPS origin;
5. ordinary restart preserves valid sessions and destructive restore/PITR cannot
   revive them;
6. graceful drain stops admission and safely completes or withholds protected
   responses;
7. logs, journals, metrics, and failures contain none of the sensitive values
   listed above;
8. the complete frontend unit and browser-acceptance suites pass for the
   deployed build. For TEST, run the deployed-origin acceptance in two passes
   around an actual `dish-service-test.service` restart so the same browser
   session proves restart survival rather than only in-process behavior;
9. the TEST frontend hostname and TEST Action path remain isolated: frontend
   routes return 404 through the Action origin, the frontend cookie is
   host-only/Secure/HttpOnly/SameSite=Strict, and Caddy supplies HSTS on the
   dedicated frontend origin.

A successful TEST run records evidence for that exact disposable candidate. It
does not satisfy, update, or imply the production activation record.

## Rollback

- Remove the dedicated HTTPS mapping without changing the public Action route.
- Disable frontend route dispatch while preserving private diagnosis/admin
  behavior.
- Revoke every frontend session by advancing the accepted security
  fence/generation.
- Preserve bounded security audit evidence without retaining plaintext
  credentials or task data.
- Leave migrations in a known forward-compatible state; downgrade only when the
  migration contract explicitly permits it and the downgrade has been rehearsed.
