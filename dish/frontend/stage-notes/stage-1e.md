# Delivery Stage 1E — fixture review environment

## Real

- A self-contained static review build with one-command startup.
- Stable URLs and an in-product switcher for every principal fixture state.
- An explicit fixture-only network boundary that rejects API and cross-origin requests.
- Review metadata in the built artifact and browser acceptance for the scenario catalogue.

## Fixture-backed

- Every board, task, notice, lifecycle, and login state remains non-canonical fixture data.
- The extreme-content scenario is deliberately synthetic and exists only for visual resilience review.

## Intentionally absent

- Authentication, sessions, cookies, CSRF, backend API routes, PostgreSQL reads, polling, and mutations.
- Any route from the review build to a production or test Dish service.

## Review entry points

Run `npm run review`, then use the Review mode scenario selector. The same states are listed with
stable paths in `review-guide.md`.
