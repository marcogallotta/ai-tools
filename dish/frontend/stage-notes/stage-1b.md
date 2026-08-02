# Delivery Stage 1B — fixture task detail and safe rendering

## Real

- Fixed-width, single-scroll, nonmodal task-detail panel that keeps the board visible.
- Native card selection, visible close control, Escape close, outside-click close, and origin-focus
  restoration.
- Structured text-only rendering with an explicit supported block registry.
- Inert bounded plain-text fallback when the renderer rejects content.
- Current project/section, factual workflow status, destination, approved human disclosures, and
  descriptive non-authorizing next-step guidance.
- Unit and browser checks plus representative rendered and fallback screenshots.

## Fixture-backed

- Detail responses and all content, disclosure, destination, status, and guidance fields are explicit
  non-canonical fixtures.
- Opening a card performs a fixture lookup; it does not claim fresh canonical retrieval.

## Intentionally absent

- Common top-banner contribution handling, lifecycle errors, refresh, routing/history, or API calls.
- Authentication, PostgreSQL data, backend renderer/sanitizer integration, and mutation authority.

## Known limitations

- Detail attention does not supersede card banner contributions until Delivery Stage 1C adds the
  shared notice system.
- Deep-link and Back/Forward behavior remain reserved for Delivery Stage 1D.
