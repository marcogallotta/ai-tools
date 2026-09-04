# External effects and Asana

## Read this when

Read this when changing Asana reads/writes, external-effect intent/attempt/observation/settlement, projection, or uncertain outcomes.

## Scope

This document records the correctness boundary around external effects and the transitional role of Asana.

## Authoritative implementation

Current anchors include `dish_tool/task_store.py`, `dish_tool/backend.py`, current effect journals/recovery code, and PostgreSQL projection/settlement code in `dish_pg/transition.py` and workers.

## Actors, processes, and stores

Before cutover, Dish reads and mutates Asana tasks and PostgreSQL target work records projection intents plus reconciliation evidence. Rollback burn is the end of Asana involvement for the activated generation: external projection is disabled and subsequent live PostgreSQL commands do not enqueue Asana projection intents. Shadow-origin work remains isolated from live effects.

## Authority and data ownership

Asana currently owns live task content/placement/completion as observed by the production workflow. It is not the intended permanent backend. PostgreSQL is intended to become canonical, while the frontend eventually replaces Asana's user-facing role.

## Invariants

- A network call is not automatically proof that an intended effect applied.
- When correctness depends on observed external state, settlement uses an authoritative observation appropriate to that effect.
- Unknown/uncertain outcomes are not blindly retried as definitely-not-applied.
- Shadow-origin work never dispatches live external effects.
- Projection/reconciliation evidence cannot silently replace backend authority.
- After rollback burn, successful live PostgreSQL commands owe no external projection intent; historical projection/reconciliation rows are forensic evidence only and cannot gate admission or frontend health.

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

While external projection is enabled, persist enough intent to recover safely, perform the external operation, observe what actually happened when needed, settle the durable outcome, and reconcile drift/uncertainty. After rollback burn, canonical PostgreSQL command success is self-contained: no new live Asana intent is created and no Asana observation/reconciliation is required to establish that success.

For pre-cutover population confidence, placement is interpreted in context rather than treated as a global synchronization invariant. Marco may legitimately move resting tasks through the ordinary external Cooking lifecycle, including later archival/history placement. A read-only population audit therefore classifies those resting/manual lifecycle differences separately from real drift. Placement becomes an inconsistency when it contradicts an active Dish operation's required/expected placement or another actual workflow invariant.

## Failure, replay, recovery, and concurrency

Pre-burn recovery continues the original effect identity. Fencing prevents stale workers from settling another attempt. Reconciliation handles discrepancies without granting external state canonical backend authority. Post-burn recovery uses PostgreSQL request/replay/audit authority; retained external-effect history may explain prior events but does not resume or recreate Asana projection.

## Change routing

New effect types must define how success, absence, and uncertainty are established. Surface-specific rendering/guidance can be changed independently; effect authority remains with the application/projection lifecycle.

## Proving tests

Current evidence includes pre-burn Asana lifecycle/effect recovery and PostgreSQL projection/reconciliation tests, plus post-burn regressions proving live commands create no new projection debt and historical projection state is not frontend health authority.

## Current debt and temporary compatibility

Asana is transitional. Architecture work should reduce, not institutionalize, dependence on Asana as the frontend/backend interface once replacement reliability is sufficient.

## Related documents

- [Authority and data ownership](authority-and-data-ownership.md)
- [PostgreSQL runtime](postgresql-runtime.md)
- [ADR-0004](decisions/0004-shadow-origin-never-projects.md)

## Archive boundary for projection effects

Before rollback burn, archive may supersede a live task projection event only while PostgreSQL can prove
that no external dispatch is in flight: `pending`/`claimed` events with no unsafe latest attempt, including
a latest `not_applied` attempt, are locked and superseded atomically. A latest `dispatched` attempt or an
`uncertain`/`blocked` event/attempt makes archive fail closed with no `archived_at` or partial cleanup.
Archive never performs a synchronous network reconciliation. After rollback burn, retained projection rows
are forensic and non-dispatchable, so they are preserved and do not become archive blockers.
