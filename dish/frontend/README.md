# Dish private frontend

This directory contains the private, read-only Dish frontend delivery work governed by
`docs/frontend.md` and `docs/frontend-imp.md`.

## Local commands

Run from `dish/frontend`:

```sh
npm run build
npm run dev
npm run lint
npm run schema:check
npm run test:unit
npm run test:browser
npm run screenshots
npm run check
```

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

Delivery Stage 0 and fixture-backed Delivery Stage 1A–1D are implemented. Real authentication and
canonical-data integration remain intentionally absent and blocked by the governing readiness gates.
