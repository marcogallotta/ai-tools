# Delivery Stage 1D — prototype interaction hardening and review packet

## Real

- Card and board keyboard navigation across cards and adjacent columns, plus keyboard-reachable
  horizontal scrolling.
- Route grammar for board and selected fixture task, push/replace behavior, Back/Forward reconciliation,
  direct fixture deep-link restoration, and route normalization without task titles in document metadata.
- Panel focus entry, Escape/outside/visible-control close, origin restoration, and column/board fallback.
- Visible focus treatment, polite versus assertive notice semantics, minimum 1024-pixel desktop layout,
  and reduced-motion verification.
- Python files now participate in source-size, whitespace, and syntax checks alongside frontend source.
- Integrated Playwright acceptance and final representative screenshots.

## Fixture-backed

- Route identities, history entries, board/detail state, retries, lifecycle outcomes, and all displayed
  task facts remain explicit non-canonical design fixtures.

## Intentionally absent

- Real authentication, session security, private service routes, canonical PostgreSQL read models,
  backend rendering, polling, and network reconciliation. These remain blocked by Gates A and B.
- Every canonical mutation and workflow-authority surface.

## Known limitations

- This is the approved fixture-backed visual prototype, not product Stage 1 acceptance.
- The browser harness can assemble the same modular source in-memory when local navigation is blocked by
  a managed Chromium policy; normal `npm run dev` serves the built modules directly.
