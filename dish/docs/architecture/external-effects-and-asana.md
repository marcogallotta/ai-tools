# External effects and Asana

## Read this when

Read this when changing Asana reads/writes, external-effect intent/attempt/observation/settlement, projection, or uncertain outcomes.

## Scope

This document records the correctness boundary around external effects and the transitional role of Asana.

## Authoritative implementation

Current anchors include `dish_tool/task_store.py`, `dish_tool/backend.py`, current effect journals/recovery code, and PostgreSQL projection/settlement code in `dish_pg/transition.py` and workers.

## Actors, processes, and stores

Dish currently reads and mutates Asana tasks. PostgreSQL target work records projection intents and uses projection/reconciliation mechanisms. Shadow-origin work is isolated from live effects.

## Authority and data ownership

Asana currently owns live task content/placement/completion as observed by the production workflow. It is not the intended permanent backend. PostgreSQL is intended to become canonical, while the frontend eventually replaces Asana's user-facing role.

## Invariants

- A network call is not automatically proof that an intended effect applied.
- When correctness depends on observed external state, settlement uses an authoritative observation appropriate to that effect.
- Unknown/uncertain outcomes are not blindly retried as definitely-not-applied.
- Shadow-origin work never dispatches live external effects.
- Projection/reconciliation evidence cannot silently replace backend authority.

## Process and transaction boundaries

```mermaid
flowchart LR
    Intent[Durable intent] --> Attempt[Owned attempt]
    Attempt --> External[External call]
    External --> Observe[Authoritative observation]
    Observe --> Settle[confirmed / not-applied / uncertain]
    Settle --> Reconcile[recovery / drift handling]
```

Not every read-only or idempotent interaction requires the same reread pattern; the observation requirement follows the actual correctness invariant of the effect.

## Normal flow

Persist enough intent to recover safely, perform the external operation, observe what actually happened when needed, settle the durable outcome, and reconcile drift/uncertainty.

For pre-cutover population confidence, placement is interpreted in context rather than treated as a global synchronization invariant. Marco may legitimately move resting tasks through the ordinary external Cooking lifecycle, including later archival/history placement. A read-only population audit therefore classifies those resting/manual lifecycle differences separately from real drift. Placement becomes an inconsistency when it contradicts an active Dish operation's required/expected placement or another actual workflow invariant.

## Failure, replay, recovery, and concurrency

Recovery continues the original effect identity. Fencing prevents stale workers from settling another attempt. Reconciliation handles discrepancies without granting external state canonical backend authority.

## Change routing

New effect types must define how success, absence, and uncertainty are established. Surface-specific rendering/guidance can be changed independently; effect authority remains with the application/projection lifecycle.

## Proving tests

Current evidence includes Asana lifecycle/effect recovery tests plus PostgreSQL projection attempt, process-failure, and reconciliation tests.

## Current debt and temporary compatibility

Asana is transitional. Architecture work should reduce, not institutionalize, dependence on Asana as the frontend/backend interface once replacement reliability is sufficient.

## Related documents

- [Authority and data ownership](authority-and-data-ownership.md)
- [PostgreSQL runtime](postgresql-runtime.md)
- [ADR-0004](decisions/0004-shadow-origin-never-projects.md)
