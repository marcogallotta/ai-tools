# Dish fixture review guide

This build is a non-canonical visual prototype. It cannot authenticate, query Dish, or mutate any
workflow state. Review mode blocks `/api/` and cross-origin requests in the browser.

## Start

From `dish/frontend`:

```sh
npm run review
```

Open the printed local URL. The command creates a fresh static build before serving it.

## Stable review paths

| State | Path |
|---|---|
| Normal board and empty column | `/?review=1&scenario=board` |
| All approved attention categories | `/?review=1&scenario=attention` |
| Rendered task detail | `/task/task-biryani?review=1&scenario=detail` |
| Safe-rendering fallback | `/task/task-aubergine?review=1&scenario=fallback` |
| Extreme content | `/task/task-extreme?review=1&scenario=extreme` |
| Zero active sections | `/?review=1&scenario=zero` |
| Loading | `/?review=1&scenario=loading` |
| Initial load failure | `/?review=1&scenario=initial-error` |
| Last-safe-view refresh failure | `/?review=1&scenario=last-safe` |
| Login shell | `/?review=1&view=login` |

## Useful feedback

Focus on information hierarchy, board density, card scanability, notice prominence, panel width,
long-content wrapping, viewport behavior, and whether factual state is easy to distinguish from an
action. Authentication behavior and real data semantics are intentionally not represented yet.
