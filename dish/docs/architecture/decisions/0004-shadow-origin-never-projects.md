# ADR: Shadow-origin work never projects

Status: Accepted

## Read this when

Read this when changing shadow execution, projection claims, external-effect enablement, recovery, or origin handling.

## Scope

Shadow-origin execution exists to produce target state and evidence. It is never eligible to dispatch live external effects.

## Authoritative implementation

Current implementation anchors live in [Dark launch](../dark-launch.md) and [External effects and Asana](../external-effects-and-asana.md). Exact module locations may change without changing this decision.

## Actors, processes, and stores

The relevant distinction is execution origin: live-origin work may enter the live effect lifecycle when otherwise authorized; shadow-origin work may not.

## Authority and data ownership

Origin is durable authority data for effect eligibility. Secondary configuration cannot promote shadow-origin work into live-effect work.

## Invariants

- Shadow-origin work cannot dispatch to Asana or another live external target.
- Effect-enable settings, epochs, worker configuration, or other misconfiguration cannot override shadow origin.
- Recovery and replay preserve origin.
- Dark-launch evidence does not transfer external-effect authority.

## Process and transaction boundaries

The origin fence must survive durable storage, worker claims, process restarts, retries, and recovery. No particular implementation module is fixed by this ADR.

## Normal flow

Shadow work may execute against the PostgreSQL target and produce comparison evidence while live external effects remain ineligible by origin.

## Failure, replay, recovery, and concurrency

Retry/recovery remains shadow-origin. Contradictory or missing secondary configuration must fail closed rather than make shadow work dispatchable.

## Change routing

Refactors may move effect code while preserving the origin fence. Any path that allows shadow-origin work to become dispatchable changes this decision.

## Proving tests

Behavioral tests should prove shadow-origin rejection at the actual effect boundary. Structural tests are appropriate where forbidden dependency/call paths themselves enforce the isolation guarantee.

## Current debt and temporary compatibility

Migration-era shadow workers and treatment metadata may change; the origin fence remains mandatory until shadow execution itself is retired.

## Related documents

- [Dark launch](../dark-launch.md)
- [External effects and Asana](../external-effects-and-asana.md)
