# ADR: Dark launch does not transfer authority

Status: Accepted

## Read this when

Read this when a change could make capture, import, shadow execution, comparison, readiness, or evidence alter live mutation authority.

## Scope

Dark launch evaluates a PostgreSQL candidate while the existing production path remains authoritative.

## Authoritative implementation

Current implementation anchors live in [Dark launch](../dark-launch.md). Exact module locations may change without changing this decision.

## Actors, processes, and stores

The relevant actors are the live service, current production stores, PostgreSQL dark-launch target, shadow workers, comparison/readiness tooling, and operators.

## Authority and data ownership

Capture, import, shadow execution, comparison, readiness, or accumulated evidence does not transfer live production mutation authority to PostgreSQL.

## Invariants

- Authority changes only through an explicit cutover decision and activation.
- Dark-launch evidence describes a candidate; it does not authorize that candidate to become live.
- Readiness or successful comparison cannot silently open mutation authority.

## Process and transaction boundaries

Every process boundary involved in dark launch must preserve the separation between evidence collection and live authority. No particular transaction-owning module is fixed by this ADR.

## Normal flow

Capture/import a bounded source state, execute and compare the PostgreSQL candidate, accumulate readiness evidence, and leave production authority unchanged until cutover is explicitly invoked.

## Failure, replay, recovery, and concurrency

Failure, retry, restart, replay, or recovery during dark launch must remain evidence-only and cannot become an implicit authority-transfer path.

## Change routing

Refactors that preserve these semantics do not require changing this ADR. A semantic change to when or how authority transfers should explicitly amend or supersede it.

## Proving tests

Behavioral and structural evidence should prove that dark-launch paths cannot mutate live authority or live external targets. Current test anchors are listed in [Dark launch](../dark-launch.md).

## Current debt and temporary compatibility

Migration-era capture/import/rehearsal mechanisms may change or disappear. Their existence does not weaken the authority boundary.

## Related documents

- [Dark launch](../dark-launch.md)
- [Authority and data ownership](../authority-and-data-ownership.md)
