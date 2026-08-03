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

Delivery Stage 0 and fixture-backed Delivery Stage 1A–1F are implemented. Real authentication and
canonical-data integration remain intentionally absent and blocked by the governing readiness gates.
## Integration readiness

- Gate A authoring review: `../docs/frontend-gate-a-readiness.md`
- Independent Gate A record: `../docs/frontend-gate-a-review.md`
- Gate B canonical-data source map: `../docs/frontend-gate-b-source-map.md`
- Independent Gate B record: `../docs/frontend-gate-b-review.md`

Gate A is not passed until an independent reviewer accepts the packet and its material findings are
resolved. Gate B is not passed until its source predicates are reconciled against the final migrated
schema and independently accepted. Delivery Stages 2 and 3 remain blocked by their respective gates.

