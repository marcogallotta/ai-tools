# Frontend Stage 3 implementation checklist

Status: prepared; execution blocked until Gates A and B pass and the database rollout is reconciled.

This checklist sequences the real board vertical slice. It forbids adapting `section_tasks()` or
`task_view()` into browser DTOs and forbids guessed attention predicates.

## 3A — accepted registries and browser identities

- record decisions for isolated tasks, lease/Verification meanings, recovery support, projection
  reducer, route identity, and cursor representation;
- land any required route-identity and recovery-support migration;
- add closed operation/phase, attention, notice, and projection-presentation registries;
- synchronize those registries with frontend OpenAPI and generated validators.

Exit evidence: `S3-IDENTITY-001`, `S3-STATUS-001`, `S3-ATTN-*`, and `S3-CONTRACT-001` are green at
unit/equivalence level.

## 3B — coherent board query service

- capture one evaluation time, active generation, active registry, labels, and section bounds;
- fetch the first bounded page for every section with set-oriented joins and one accepted open
  operation;
- bulk derive accepted attention/projection facts for returned candidates;
- build closed DTOs, notices, snapshot identity, continuity inputs, and explicit empty/configuration/
  capacity/service outcomes after the read transaction closes.

Exit evidence: `S3-BOARD-*`, `S3-ORDER-001`, `S3-STATUS-001`, and `S3-SURFACE-001` are green.

## 3C — continuation and retry safety

- implement bounded section continuation under the accepted cursor design;
- bind cursor state to environment, type, section, ordering, page size, contract, continuity, expiry,
  and cleanup requirements;
- normalize current route identities only through accepted bootstrap state;
- reset only the affected column after cursor invalid/stale outcomes.

Exit evidence: `S3-CURSOR-*` and `S3-SNAPSHOT-001` are green, including lost-response retry.

## 3D — query plans, API integration, and browser switch

- add only indexes justified by final PostgreSQL plans;
- record query count, statement timeout, response bound, cold/warm timing, and minimum/typical/maximum
  `EXPLAIN (ANALYZE, BUFFERS)` evidence;
- implement board/bootstrap and continuation handlers through the frontend principal;
- switch the protected board from fixtures to canonical DTOs while leaving task detail explicitly
  placeholder-only until Stage 4.

Exit evidence: `S3-BOUNDS-*`, HTTP contract tests, real-data Playwright review, and Gate C record are
green.

## Stage 3 handoff record

Record the exact activated migration/schema fingerprint, registry versions, predicate decisions,
indexes, query plans, configured limits, build, Gate B review, and tests run. Any unmapped fact reopens
Gate B rather than becoming an implementation guess.
