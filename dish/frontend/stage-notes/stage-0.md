# Delivery Stage 0 — foundation and empty shell

## Real

- Modular HTML, CSS, JavaScript, API, feature, shell, tooling, fixture, and test boundaries.
- Reproducible build, development, formatting, lint, source-size, OpenAPI, generated-client, unit-test,
  Playwright/Chromium browser-test, and screenshot commands.
- Separate frontend-only OpenAPI document and generated client synchronization check.
- Runnable login shell and protected empty application shell.
- Repository pytest bridge for the normal Dish test command.

## Fixture-backed

- The shell selector is a local review convenience only.
- Both shells carry an explicit fixture-prototype label.

## Intentionally absent

- Authentication, sessions, cookies, CSRF, server routing, and private-origin integration.
- Board columns, cards, task detail, notices, refresh, pagination, and canonical task data.
- Any command, mutation, workflow-policy, placement, completion, or projection authority.

## Known limitations

- Chromium is required for browser checks and screenshot generation; the Node/unit/tooling lane remains
  runnable without it.
- The OpenAPI document is a frontend-owned synchronization target, not a claim that service routes
  already exist.
