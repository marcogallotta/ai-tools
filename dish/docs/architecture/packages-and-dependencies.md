# Packages and dependencies

## Read this when

Read this when a change crosses package boundaries or when current responsibility is unclear.

## Scope

This is a descriptive package map and dependency-direction guide. It does not freeze filenames, composition roots, or helper placement.

## Authoritative implementation

Current packages are `dish_tool/`, `dish_service/`, `dish_shadow/`, `dish_pg/`, `test_selection/`, plus entry points and scripts. Agent/admin CLI presentation implementations live in `dish_service/cli.py` and `dish_service/admin_cli.py`; `dish_tool` retains the lower application/domain components they consume.
Reusable identifier grammar and narrow Asana task-URL parsing are lower-layer primitives owned by `dish_tool/identifiers.py` and `dish_tool/task_urls.py`; service transports and clients consume those primitives rather than owning them.

## Actors, processes, and stores

`dish_tool` contains much of the current legacy/application/domain behavior; `dish_service` composes service/transport concerns; `dish_pg` implements the PostgreSQL target; `dish_shadow` contains dark-launch treatment policy; `test_selection` owns test-routing metadata.

## Authority and data ownership

A package may consume another package's authority without becoming that authority. Shared behavior should be factored where responsibility is clearest, not automatically pushed to the "highest" common layer.

## Invariants

- Dependency direction should make authority understandable locally.
- Cross-package reuse must not create a competing durable writer or workflow authorization path.
- Transport/presentation packages may contain substantial surface-specific logic.
- Current package/module locations are refactorable when ownership becomes clearer.
- Shadow code must not become a live-effect authority.

## Process and transaction boundaries

Package boundaries do not imply transaction boundaries. Transactions belong to the runtime operation that must be atomic; implementations may evolve as long as the durability/recovery invariants remain explicit.

## Normal flow

Domain/application decisions are consumed by persistence, transport, projection, and presentation adapters. Adapters may normalize, filter, group, render, or add surface-specific affordances without independently authorizing workflow transitions.

## Failure, replay, recovery, and concurrency

Shared recovery identities and authoritative outcomes must survive package refactors. A dependency cleanup must not create a second retry loop, writer, or authority.

## Change routing

Prefer the component that naturally owns the responsibility. Avoid generic dumping-ground modules, but do not ban legitimate reusable helpers. Scripts should normally call existing authorities rather than recreate domain state machines.

## Proving tests

Behavioral tests should protect authority and contracts; structural tests should be used sparingly where code topology itself is a real safety boundary. Relevant current tests include `tests/test_dish_transaction_ownership.py` and PostgreSQL command/runtime tests.

## Current debt and temporary compatibility

Legacy and PostgreSQL packages coexist, so some imports and adapters are transitional. Stage B cleanup is expected to change current module boundaries; this document must not obstruct that work by treating today's composition root as permanent.

## Related documents

- [System context](system-context.md)
- [Authority and data ownership](authority-and-data-ownership.md)
- [Extension rules](extension-rules.md)
