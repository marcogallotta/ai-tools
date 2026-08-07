# ADR: Cutover evidence is bounded

Status: Accepted

## Read this when

Read this when changing cutover certification, candidate identity, readiness evidence, source capture, reconciliation, or long-term evidence retention.

## Scope

Cutover needs enough evidence to protect authority transfer, recovery, and first admission without creating permanent open-ended certification bureaucracy.

## Authoritative implementation

Current implementation anchors live in [PostgreSQL runtime](../postgresql-runtime.md), [Dark launch](../dark-launch.md), and the cutover program. Exact module locations may change without changing this decision.

## Actors, processes, and stores

Evidence is produced by source capture, import, reconciliation, runtime/recovery tests, readiness checks, and operator review for a specific cutover candidate.

## Authority and data ownership

Evidence certifies a specific candidate/revision and the source, target generation, schema, corpus, and other identity facts that make that certification meaningful.

## Invariants

- Evidence for one candidate/revision/source/schema/generation/corpus does not automatically certify another.
- Material identity changes or newly discovered material gaps invalidate the affected evidence.
- Required durable evidence remains limited to what protects authority transfer, recovery, and first admission.
- Evidence is not itself authority to cut over.

## Process and transaction boundaries

Evidence may be collected across multiple processes and runs; its identity binding must survive those boundaries. The ADR does not require a particular report format or commit topology.

## Normal flow

Collect the required evidence for a named candidate, bind it to the relevant identities, resolve material gaps, make the explicit cutover decision separately, and retain only the concise durable record needed afterward.

## Failure, replay, recovery, and concurrency

Changed candidate/source/schema/generation/corpus identity or a newly discovered material gap cannot silently inherit prior certification; affected evidence must be re-established.

## Change routing

Refactors may change how evidence is collected or stored. Changes to what must be re-established after identity/gap changes, or to the bounded-retention principle, require architectural review.

## Proving tests

Tests and rehearsals should prove candidate binding, fail-closed invalidation after material identity changes, recovery readiness, and separation between evidence and authority transfer.

## Current debt and temporary compatibility

Migration-era evidence tooling may currently be larger than the desired end state. It should not become permanent solely because it exists today.

## Related documents

- [PostgreSQL runtime](../postgresql-runtime.md)
- [Dark launch](../dark-launch.md)
