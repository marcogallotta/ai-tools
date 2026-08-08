# Dish private frontend

This directory contains the private, read-only Dish frontend delivery work governed by
`docs/frontend.md` and `docs/frontend-imp.md`.

## Local commands

Run from `dish/frontend`:

```sh
npm run build
npm run dev
npm run review
npm run lint
npm run schema:check
npm run test:unit
npm run test:browser
npm run screenshots
npm run check
```

`npm run review` creates a fresh fixture-only static build and serves the stable visual-review scenarios documented in `review-guide.md`. Review mode rejects backend and cross-origin browser requests.

The implementation uses browser-native ES modules, Node's built-in unit-test runner, and a
Playwright browser harness that drives the installed Chromium executable.

## Boundaries

- `src/js/api/` owns frontend API transport and generated/schema-checked code.
- `src/js/features/` owns product features behind small interfaces.
- `src/js/shell/` owns the application and login shells.
- `src/styles/` separates tokens, base rules, layout, and component rules.
- `fixtures/` contains visibly non-canonical prototype data and is not an authority source.
- `tests/` contains unit and browser-level behavior checks.
- `openapi/` contains the frontend-only OpenAPI contract; it is separate from Action schemas.
- `stage-notes/` and `screenshots/` contain delivery evidence.

No Stage 0 or Stage 1 fixture is canonical task data. No frontend route or component is a workflow,
placement, completion, projection, or content-mutation authority.

## Current delivery status

Delivery Stage 0 and fixture-backed Delivery Stage 1A–1F are implemented. The Stage 3 PostgreSQL
read core can now be exercised through an explicit loopback-only local observation mode. Real
authentication, production frontend routing, and authoritative PostgreSQL activation remain absent
and governed by their readiness gates.
## Integration readiness

- Gate A authoring review: `../docs/frontend-gate-a-readiness.md`
- Independent Gate A record: `../docs/frontend-gate-a-review.md`
- Gate B canonical-data source map: `../docs/frontend-gate-b-source-map.md`
- Independent Gate B record: `../docs/frontend-gate-b-review.md`

Gate A is not passed until an independent reviewer accepts the packet and its material findings are
resolved. Gate B is not passed until its source predicates are reconciled against the final migrated
schema and independently accepted. The local Stage 3 PostgreSQL observation path does not pass or
bypass either gate and is not production activation.


## Pre-integration contracts

`contracts/` contains machine-checked plans for blocked future integration. They are not runtime
configuration or authority:

- `stage2-security-contract.json` records implementation-local authentication/runtime decisions;
- `stage3-read-contract.json` reconciles the board source map to the checked-in PostgreSQL head;
- `stage2-acceptance-cases.json` and `stage3-acceptance-cases.json` reserve stable acceptance IDs;
- `pre-db-readiness.json` records the current go/no-go boundary.

The unit suite checks these files against the frontend OpenAPI and checked-in model/migration source so
schema or contract drift must be reconciled explicitly.

## Local PostgreSQL observation board

The Stage 3 board can be served locally from PostgreSQL without changing production routing or
backend authority. This is an observation/offload surface only: SQLite and Asana remain authoritative
until an explicit cutover, and the local server exposes no mutation routes.

Start the repository Compose PostgreSQL target from `dish/`:

```sh
DISH_POSTGRES_DB=dish_stage_a_test \
DISH_POSTGRES_USER=dish \
DISH_POSTGRES_PASSWORD=dish \
DISH_POSTGRES_HOST_PORT=55432 \
  docker compose -f deploy/postgresql/compose.yaml up -d
```

Apply the migration files actually present in the checkout, build the frontend, and start the
loopback-only local server:

```sh
.venv/bin/alembic -c alembic.ini upgrade head
npm --prefix frontend run build
.venv/bin/python scripts/dish-frontend-local
```

Open `http://127.0.0.1:4173/?source=postgresql`. Without `source=postgresql` the same build remains
fixture-backed. `review=1` always forces fixture mode and blocks frontend API requests.

The local server defaults to `postgresql+psycopg://dish:dish@127.0.0.1:55432/dish_stage_a_test`.
Override it only for another intentional local target with `DISH_FRONTEND_LOCAL_DATABASE_URL` or
`--database-url`. The local delayed-projection display threshold defaults to 900 seconds and may be
overridden with `DISH_FRONTEND_LOCAL_PROJECTION_DELAY_SECONDS`; this local value is not acceptance of
the outstanding production projection-delay contract.

An already-populated local database can be used immediately. The repository-supported real refresh
path remains legacy SQLite/location evidence through `scripts/dish-pg-export-legacy`, then
`scripts/dish-pg-bootstrap-initial`, then `scripts/dish-pg-import-legacy`. Bootstrap is
empty-target-only; test fixture helpers are not runtime population tooling.

With the server running against a populated local database, exercise the real browser path with:

```sh
DISH_FRONTEND_LOCAL_URL='http://127.0.0.1:4173/?source=postgresql' \
  .venv/bin/python frontend/tools/browser_harness.py local-postgresql
```
