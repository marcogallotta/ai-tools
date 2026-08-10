# Delivery Stage 6 — accessibility and integration hardening

## Real in this implementation pass

- Preserves the already-integrated canonical browser route shape
  `/dishes/<stored-dish-uuid>/<decorative-title-slug>`; no legacy `/tasks` browser route was added.
- Preserves real Asana section display names through the read-only location-manifest → legacy-export →
  initial-bootstrap path, so ordinary frontend presentation no longer needs `Imported section <gid>`
  placeholders for newly exported source bundles. Workflow roles remain explicit bootstrap metadata
  and are not inferred from section names.
- Collapses the canonical `PROCESS RECORD` portion of task content by default while keeping the actual
  recipe/canonical content immediately visible. Existing technical disclosures remain separately
  collapsed.
- Suppresses neutral no-active-operation card text, keeps pending-review attention prominent, and
  retains affected-task links in grouped attention notices.
- Completes the current accessibility hardening pass: one main landmark, dedicated live regions rather
  than a live application root, reduced-motion-aware horizontal keyboard scrolling, per-section busy
  state during continuation loads, deterministic focus recovery when refreshed cards disappear, and
  stronger focus/muted-text contrast.
- Keeps exactly one main landmark in the login and unresolved-logout shells as well as the protected
  application shell.
- Makes board keyboard installation idempotent on the persistent board host, preventing background or
  manual board re-renders from accumulating duplicate `keydown` handlers.
- Splits the fixture visual-review build from the production-shaped frontend. Production `dist/`
  contains no fixture payloads, prototype/review modules, review stylesheet, fixture browser routes,
  or fixture-backed runtime fallback; `npm run review` builds those assets separately in
  `review-dist/`.
- Repairs the Stage 5 cursor browser harness for the current UUID task identity contract and extends it
  to cover continuation busy state and refresh focus fallback.

## Deliberately not added

- No admin interface, global/title search, card state colour system, or redesign of the aggregate
  attention model. Those are later product work.
- No mutation controls, workflow-policy changes, authority changes, cutover controls, or Stage 7
  production-shaped browser-acceptance expansion.
- No legacy browser-route compatibility layer.

## Gate status

This is the Stage 6 implementation/hardening pass, not a claim that the Stage 6 human walkthrough or
frontend Gates A/B have been accepted. Delivery Stage 7 remains a separate gate.

## Automated evidence

- `npm --prefix frontend run check`: passed (73 frontend unit tests plus format/lint/schema/build).
- `npm --prefix frontend run test:browser`: passed.
- Planner-selected focused Python lane: 48 passed; the only failure is the pre-existing unclassified
  `docs/architecture/decisions/0006-cutover-bar-matches-operating-context.md`, reproduced unchanged
  against the reviewed Stage-6 baseline.
- Governed smoke lane: all 462 collected smoke tests passed. The sandbox's single-command timeout was
  shorter than the complete lane, so the exact collected node set was executed in deterministic
  partitions.
- Ordinary full-suite collection: 2293 passed, 71 skipped, 22 failed, 37 deselected when the exact
  2386 selected node IDs were executed in deterministic partitions after the single-process run
  exceeded the sandbox command window. The 22 failing test IDs are exactly the same 22 recorded by
  the reviewed Stage-6 baseline; this re-review fix adds no new full-suite failures.
- Native PostgreSQL certification was not runnable in the supplied environment: no PostgreSQL DSN or
  server/client runtime is available. No native-PG pass is claimed.
