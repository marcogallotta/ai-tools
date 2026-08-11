# Frontend deployment runbook

Status: the authenticated frontend is implemented but disabled by default.
Gate A and the applicable Gate B scope must pass before exposure.

This runbook owns deployment and rollback. Product behavior remains in
`frontend.md`, the access contract remains in `frontend-imp.md`, and acceptance
decisions remain in the Gate A/B records.

## Entry conditions

- Gate A records PASS for the exact build and schema revision.
- Gate B records PASS for every PostgreSQL-backed board/detail field being activated.
- Native PostgreSQL, destructive restore/PITR, browser lifecycle, and query-plan
  evidence is accepted.
- The target is explicit. Test is for rehearsal; production is the default for
  genuine Dish work and requires Marco's explicit authorization for production
  administration.

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
   required frontend value.
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

6. Configure the dedicated HTTPS mapping, then enable authentication. Enable
   PostgreSQL reads only when the separately reviewed Gate B scope and
   projection-delay setting are accepted.

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
5. ordinary restart preserves valid sessions and destructive restore/PITR
   cannot revive them;
6. graceful drain stops admission and safely completes or withholds protected
   responses;
7. logs, journals, metrics, and failures contain none of the sensitive values
   listed above;
8. the complete frontend unit and Stage 7 browser-acceptance gates pass for the
   deployed build.

## Rollback

- Remove the dedicated HTTPS mapping without changing the public Action route.
- Disable frontend route dispatch while preserving private diagnosis/admin behavior.
- Revoke every frontend session by advancing the accepted security fence/generation.
- Preserve bounded security audit evidence without retaining plaintext
  credentials or task data.
- Leave migrations in a known forward-compatible state; downgrade only when
  the migration contract explicitly permits it and the downgrade has been
  rehearsed.
