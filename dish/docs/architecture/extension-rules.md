# Extension rules

## Read this when

Read this when a change alters a durable authority, cross-package contract, external-effect lifecycle, or other architectural boundary. Small localized implementation changes generally do not need this document.

## Scope

This document gives principles for evolving architecture. It is not an agent checklist and does not dictate exact files, commit structure, or mandatory testing ceremony.

## Authoritative implementation

Current subsystem anchors are listed in [the architecture index](index.md). They are descriptive current locations.

## Actors, processes, and stores

Architecture changes are implemented by maintainers/agents but governed by runtime authority and product/safety invariants, not by a preferred coding topology.

## Authority and data ownership

The core question is: which component is authoritative for the durable fact or consequential decision? Shared consumers may contain their own logic while deriving authoritative semantics from that boundary.

## Invariants

- Preserve explicit product/safety guarantees unless intentionally changed.
- Avoid competing authoritative writers/decision engines for the same durable fact.
- Do not infer that "not authoritative" means "contains no logic."
- Prefer locally understandable abstractions; avoid both copy-pasted policy and abstraction for abstraction's sake.
- Treat current modules/packages as refactorable implementation unless an ADR says otherwise.
- Temporary compatibility should have a concrete reason to exist and a plausible retirement condition.

## Process and transaction boundaries

For changes involving durability, concurrency, or external effects, understand who executes the operation, what must be atomic/durable, and how failure is recovered. This level of analysis is not required for unrelated formatting/read-only/presentation changes.

## Normal flow

Identify the actual invariant, inspect the relevant current owner and consumers, make the smallest coherent change, and update architecture only when the architectural boundary itself changed.

## Failure, replay, recovery, and concurrency

Do not create a second replay identity, legal-transition authority, effect retry authority, lease authority, or canonical writer. Surface-specific filtering, presentation, navigation, and diagnostics are not competing authority merely because they contain decisions.

## Change routing

There is deliberately no permanent "change X only in file Y" table here. Current anchors live in the domain documents. Choose placement based on responsibility and current architecture; when a change alters the architecture, update the descriptive map.

## Proving tests

Prefer behavioral tests at the boundary that matters. Use structural tests only for genuine structural safety properties, not to freeze implementation layout or prose.

## Current debt and temporary compatibility

This file replaces earlier prescriptive routing doctrine. Contributor/agent process belongs in `CLAUDE.md`, `AGENTS.md`, and testing/runbook documentation.

## Related documents

- [Architecture index](index.md)
- [Packages and dependencies](packages-and-dependencies.md)
- [Testing boundaries](testing-boundaries.md)
