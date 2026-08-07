# ADR-0005: Cutover evidence is bounded

Status: Accepted

## Read this when
Adding release gates, candidate evidence, cutover certification, or permanent operational bureaucracy.

## Scope
This decision owns the shape of evidence required to transfer authority safely without turning one cutover into an indefinite certification system.

## Authoritative implementation
`dish_pg/release_evidence.py`, `dish_pg/release_status.py`, `dish_pg/release.py`, `dish_pg/cutover_control.py`, `scripts/dish-pg-release`.

## Actors, processes, and stores
Release tooling collects exact evidence; Marco approves the bound candidate; cutover control records checkpoints/admission.

## Authority and data ownership
Evidence is candidate/revision-specific and recomputed from authoritative stores/artifacts. It is not a free-form checklist assertion.

## Invariants
Stale or mismatched evidence cannot be relabeled; only facts needed for authority transfer, recovery, and first admission are permanent gates.

## Process and transaction boundaries
Offline collection precedes approval; cutover checkpoints and rollback burn are durable target transactions.

## Normal flow
Build candidate, collect bounded evidence, validate, approve, execute cutover, retain concise proof.

## Failure, replay, recovery, and concurrency
Any changed source/schema/generation/corpus/gap invalidates the bound candidate and requires new evidence.

## Change routing
Add a gate only when it protects a concrete authority, recovery, or first-admission invariant.

## Proving tests
`tests/postgresql/test_release_evidence_contracts.py`, `tests/postgresql/test_stage6_release_cutover.py`, `tests/postgresql/test_stage8_cutover_evidence_gates.py`.

## Current debt and temporary compatibility
The repository contains extensive migration/rehearsal planning documents; they are provenance and active planning, not permanent runtime certification authority.

## Related documents
[PostgreSQL runtime](../postgresql-runtime.md), [Testing boundaries](../testing-boundaries.md).
