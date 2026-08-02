# Delivery Stage 1C — notices and application lifecycle states

## Real

- One stacked top-of-screen banner system with severity text and appropriate alert/status semantics.
- Distinct-task grouping and truthful counts derived only from currently loaded fixture contributions.
- Fresh open-detail attention supersedes the selected card contribution; fallback rendering contributes
  a common warning until the panel closes.
- Persistent loading shell, initial-load failure shell with explicit retry, and last-safe-view behavior
  that keeps the usable board visible under a refresh warning.
- Reduced-motion handling for the loading presentation.
- Unit and browser evidence plus screenshots of grouped warnings and lifecycle states.

## Fixture-backed

- Task contributions, lifecycle errors, successful retry, and last-safe board are deterministic local
  scenarios. They do not represent network requests or session outcomes.

## Intentionally absent

- Polling, stale-response rejection, real service/session error classification, cross-tab behavior,
  and canonical refresh reconciliation.
- Authentication, PostgreSQL reads, and mutation authority.

## Known limitations

- The prototype demonstrates the approved state treatments but does not run a background timer.
- URL/history and full keyboard hardening remain in Delivery Stage 1D.
