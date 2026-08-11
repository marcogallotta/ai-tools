# Dish private frontend

This directory contains the private, read-only Dish frontend. Product behavior is governed by
`docs/frontend.md`; durable system boundaries are routed through `docs/architecture/index.md`.

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

`npm run build` creates the production-shaped `dist/` tree. It contains no fixture payloads,
prototype application, or review catalogue. `npm run review` instead creates and serves a separate
fixture-only `review-dist/` tree for the stable visual-review scenarios documented in
`review-guide.md`; review mode rejects backend and cross-origin browser requests.

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
- `screenshots/` contains fixture-review images, not current contracts.

No Stage 0 or Stage 1 fixture is canonical task data. No frontend route or component is a workflow,
placement, completion, projection, or content-mutation authority.

## Current status

The authenticated shell, PostgreSQL board/detail reads, canonical Dish-UUID deep links,
refresh/reconciliation, accessibility behavior, production/review build split, and browser suite
are implemented. Production activation remains disabled pending the exact-candidate decision in
`../docs/frontend-activation.md`. PostgreSQL remains non-authoritative until the separate cutover
process explicitly transfers authority.

## Local PostgreSQL observation frontend

The Stage 3 board and Stage 4 read-only task detail can be served locally from PostgreSQL without
changing production routing or backend authority. This is an observation/offload surface only:
SQLite and Asana remain authoritative until an explicit cutover, and the local server exposes no
mutation routes.

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

Open `http://127.0.0.1:4173/?source=postgresql`. The production-shaped build requires that explicit
local PostgreSQL source selection; it no longer falls back to fixtures. Fixture review is available
only through the separate `npm run review` build. In PostgreSQL mode, selecting a task opens fresh
read-only detail and normalizes the URL to
`/dishes/<stored-dish-uuid>/<decorative-title-slug>?source=postgresql`; the stored Dish UUID is
authoritative and the slug is decorative. Direct load/reload and Back/Forward restore that local
detail state. The UUID is an identifier, not an authorization credential. Destination remains
omitted until an accepted canonical source is recorded.

The local server defaults to `postgresql+psycopg://dish:dish@127.0.0.1:55432/dish_stage_a_test`.
Override it only for another intentional local target with `DISH_FRONTEND_LOCAL_DATABASE_URL` or
`--database-url`. The local delayed-projection display threshold defaults to 900 seconds and may be
overridden with `DISH_FRONTEND_LOCAL_PROJECTION_DELAY_SECONDS`; this local value is not acceptance of
the outstanding production projection-delay contract.

An already-populated local database can be used immediately. The repository-supported real refresh
path remains legacy SQLite/location evidence through `scripts/dish-pg-export-legacy`, then
`scripts/dish-pg-bootstrap-initial`, then `scripts/dish-pg-import-legacy`. Bootstrap is
empty-target-only; test fixture helpers are not runtime population tooling.

With the server running against a populated local database, exercise the real board/detail browser
path (including detail open, deep-link reload, canonical Dish UUID routing, and close) with:

```sh
DISH_FRONTEND_LOCAL_URL='http://127.0.0.1:4173/?source=postgresql' \
  .venv/bin/python frontend/tools/browser_harness.py local-postgresql
```
