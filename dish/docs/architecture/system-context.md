# System context

## Read this when

Read this when changing deployable processes, trust boundaries, mutation entry points, or the relationship among frontend, service, Asana, SQLite, and PostgreSQL.

## Scope

This document describes system actors and runtime boundaries. It does not prescribe file placement or developer workflow.

## Authoritative implementation

Current anchors include `dish_service/__main__.py`, `dish_service/http.py`, `dish_service/auth.py`, `dish_tool/database.py`, `dish_tool/task_store.py`, `dish_pg/postgres_service.py`, and dark-launch components under `dish_pg/` and `dish_service/`.

## Actors, processes, and stores

```mermaid
flowchart LR
    Agent[Agent CLI / MCP] --> S[Dish service]
    Admin[Admin CLI] --> S
    GPT[GPT Action] --> S
    Frontend[Frontend] --> S
    Honest[Honest assets] --> S
    S --> PG[(PostgreSQL canonical authority)]
    S -. legacy / forensic compatibility .-> Legacy[SQLite / archived Asana]
```

The important boundary is the Dish service authority, not a particular client. CLI, GPT Action, admin tools, and frontend may have different presentation, capability, and authentication logic while sharing the same mutation authority.

The current frontend is a private, read-only human surface. The browser talks only to the
existing private service listener through a dedicated HTTPS origin; frontend paths remain absent
from the public Action listener. The browser never connects to PostgreSQL or Asana directly.

## Authority and data ownership

Clients do not own durable workflow truth. The service coordinates canonical PostgreSQL state and workflow. The production cutover has completed; archived Asana Cooking and SQLite workflow state are legacy/forensic compatibility only and cannot promote themselves back into live authority.

## Invariants

- All live mutations, including frontend mutations, go through the shared Dish service authority rather than a second writable backend path.
- Authentication/authorization are explicit per caller/surface; capability overlap between surfaces is allowed.
- Frontend browser sessions authorize only frontend reads and their own lifecycle. They are not
  agent, admin, or Action credentials and confer no mutation capability.
- Clients do not receive database paths or backend credentials.
- Historical dark-launch/shadow evidence does not transfer or supersede current PostgreSQL authority.
- Legacy/shadow-origin work cannot cause live external effects.

## Process and transaction boundaries

Transport processes authenticate and validate their own protocol concerns, then invoke application/runtime authority. The architecture does not require transports to be logic-free; it requires them not to become competing authorities for workflow transitions or durable facts.

## Normal flow

A caller authenticates, submits a command, the service evaluates authoritative state and policy, persists required intent/evidence, performs any governed effect, and returns a result adapted to that caller's surface.

## Failure, replay, recovery, and concurrency

Failures are handled by the replay/lease/effect mechanisms described in the linked documents. A transport failure does not create a second state authority.

## Change routing

Changing client UX, response guidance, parsing, capability display, or transport adaptation may legitimately happen in that surface. Changing the authoritative legality or durable result of a workflow transition belongs to the workflow/application authority. Do not infer permanent module placement from this description.

## Proving tests

Current relevant evidence includes `tests/test_action_surface.py`, `tests/test_transport_contract_resilience.py`, and PostgreSQL production-shaped/runtime contract tests under `tests/postgresql/`.

## Current debt and temporary compatibility

PostgreSQL is the current production authority. SQLite/Asana migration artifacts remain only where concrete compatibility, forensic, or recovery needs justify them; the frontend remains a separate presentation/access surface.

## Related documents

- [Authority and data ownership](authority-and-data-ownership.md)
- [Commands and surfaces](commands-and-surfaces.md)
- [PostgreSQL runtime](postgresql-runtime.md)
- [Dark launch](dark-launch.md)
