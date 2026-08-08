# Frontend Stage 3 implementation checklist

Status: read-core implementation and loopback-only local observation wiring are in progress behind a non-production boundary; production/private HTTP/browser activation remains blocked until Gate B and runtime evidence pass.

This checklist sequences the real board vertical slice. PostgreSQL may be read during dark launch as
a non-authoritative observation/offload surface; that does not transfer task/workflow authority. It
forbids adapting `section_tasks()` or `task_view()` into browser DTOs and forbids guessed attention
predicates.

## 3A — accepted registries and browser identities

- record the accepted isolated-task visibility decision and preserve unresolved invalid/contested
  lease and failed/disputed Verification meanings without guessing;
- use stateless typed route identities and stateless retry-safe cursors, so Stage 3 currently requires
  no persistence migration;
- map only durable recovery/projection facts supported by current head and keep unaccepted reducer
  branches gated;
- add closed operation/phase and attention registries and synchronize them with frontend OpenAPI and
  generated validators.

Exit evidence: `S3-IDENTITY-001`, `S3-STATUS-001`, `S3-ATTN-*`, and `S3-CONTRACT-001` are green at
unit/equivalence level.

## 3B — coherent board query service

- capture one evaluation time, active generation, active registry, labels, and section bounds;
- reject section-count and registry/configuration-fatal conditions from the cheap metadata read before
  issuing the bootstrap card/attention query;
- fetch the first bounded page for every section with set-oriented joins and one accepted open
  operation;
- bulk derive accepted attention/projection facts for returned candidates;
- build closed DTOs, notices, snapshot identity, continuity inputs, and explicit empty/configuration/
  capacity/service outcomes after the read transaction closes.

Exit evidence: `S3-BOARD-*`, `S3-ORDER-001`, `S3-STATUS-001`, and `S3-SURFACE-001` are green.

## 3C — continuation and retry safety

- implement bounded section continuation under the accepted cursor design;
- bind cursor state to environment, type, section, ordering, page size, contract, continuity, and
  expiry; the current design is stateless and has no cursor-store cleanup lifecycle;
- expose only typed route identities while keeping immutable task/section UUIDs internal;
- reset only the affected column after cursor invalid/stale outcomes.

Exit evidence: `S3-CURSOR-*` and `S3-SNAPSHOT-001` are green, including lost-response retry.

## 3D — native plans, production API integration, and browser switch

- use the separately authorized loopback-only local PostgreSQL observation harness to gather real-data
  usability evidence; the harness itself is not 3D activation or authority transfer;
- add only indexes justified by final PostgreSQL plans;
- record query count, transaction isolation/coherence, statement timeout, response bound, cold/warm
  timing, and minimum/typical/maximum `EXPLAIN (ANALYZE, BUFFERS)` evidence;
- after Gate B passes for board scope, implement board/bootstrap and continuation handlers through the
  frontend principal;
- switch the protected board from fixtures to canonical DTOs while leaving task detail explicitly
  placeholder-only until Stage 4.

Exit evidence: `S3-BOUNDS-*`, HTTP contract tests, real-data Playwright review, and Gate C record are
green.

## Stage 3 handoff record

Record the exact activated migration/schema fingerprint, registry versions, predicate decisions,
indexes, query plans, configured limits, build, Gate B review, and tests run. Any unmapped fact reopens
Gate B rather than becoming an implementation guess.
