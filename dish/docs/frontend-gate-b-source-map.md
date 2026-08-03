# Frontend Gate B canonical-data source map

## Status

**Authoring map complete for the Stage 3 board and Stage 4 detail scope; Gate B is not passed.**

This packet maps the approved frontend fields against the current PostgreSQL models, read surfaces,
and frontend contracts. It is deliberately written before the pending production database rollout is
complete so that the rollout can absorb missing read support and indexes. The map must be reconciled
against the exact migrated schema and independently reviewed before Delivery Stage 3 begins. The
Stage 4 portion must be reviewed again immediately before Delivery Stage 4.

No predicate marked **unresolved** below may be guessed in a query, browser component, label mapper,
or DTO builder. A Stage 3 implementation may begin only after all board predicates are accepted and
the review record in `frontend-gate-b-review.md` records a pass for that scope.

## Evidence inspected

- Product and implementation contracts: `frontend.md`, `frontend-imp.md`.
- PostgreSQL authority models: `dish_pg/models.py`, `dish_pg/stage3_models.py`,
  `dish_pg/stage5_models.py`, and `dish_pg/stage6_models.py`.
- Current PostgreSQL read surface: `dish_pg/read_model.py`.
- PostgreSQL transition and projection code under `dish_pg/`.
- Database design, implementation, migration, testing, and production-ledger documents in `docs/`.
- Workflow-policy and recovery implementation under `dish_tool/`, used only to identify current
  authority concepts; those Python paths are not approved as per-card query loops.
- Fixture frontend DTO shapes, notice registry, detail fixtures, and frontend OpenAPI document.

The current checked-in models are treated as design evidence, not proof that the same schema is live in
production. The migration reconciliation checklist below is mandatory.

## Material findings blocking Gate B

| ID | Finding | Required resolution |
|---|---|---|
| B-01 | The pending PostgreSQL rollout has not been reconciled against this map. | Record the exact migration/schema revision and verify every named table, constraint, index, and lifecycle value after rollout. |
| B-02 | `PostgresReadModel.section_tasks()` is a per-section list query, includes completed tasks, exposes raw technical/external identities, and cannot produce a coherent all-section bootstrap with attention facts. | Add a frontend-owned board query service and DTO builder; do not extend the browser from this method directly. |
| B-03 | The current read model has no browser-safe task/section route-identity authority. | Add a bounded environment/type-scoped route-identity codec or frontend-owned alias mapping with normalization tests. |
| B-04 | The English terms **invalid lease** and **contested lease** have no exact named PostgreSQL predicate in the current schema. | Add a versioned frontend predicate registry backed by accepted durable facts, or approve a targeted contract amendment. |
| B-05 | Verification **failed** and **disputed** do not resolve unambiguously from `VerificationCycle.lifecycle/outcome`; approved outcome values and dispute authority are not defined as frontend predicates. | Name the exact lifecycle/outcome/review predicates and add equivalence tests against governing workflow behavior. |
| B-06 | A **named unresolved recovery requirement** has no single durable PostgreSQL relation in the current model set. Several command/recovery paths compute transient `recovery_required` results, but those are not a canonical set-oriented task fact. | Add frontend-owned durable/read-support state derived transactionally from governing recovery evidence, or amend the contract. |
| B-07 | Projection presentation lacks an accepted state reducer for `delayed`, `failed`, `drifted`, `unknown`, `unavailable`, `current`, and `not_configured`, including configured delay thresholds and precedence. | Add one versioned projection-presentation reducer over durable projection facts and readiness input, with threshold configuration and equivalence tests. |
| B-08 | The current `task_view()` returns `legal_actions`, raw UUID-backed internal data, and a hard-coded `not_configured` projection result; it performs multiple scalar workflow queries. | Add a dedicated frontend detail query/factual service. Do not serialize `TaskCurrentView` to the browser. |
| B-09 | No checked-in versioned attention, disclosure, advisory, or projection-presentation backend registry exists. | Add registries synchronized with the frontend OpenAPI contract and generated browser validators. |
| B-10 | Required bounded-query, query-plan, response-size, and execution-time evidence does not yet exist for board bootstrap, continuation, or detail. | Land plan/performance fixtures and enforce configured limits with closed capacity errors. |
| B-11 | Browser-facing board snapshot, section continuity, and bounded retry-safe cursor semantics are not implemented. | Add frontend-specific identity/cursor services bound to all contract-relevant inputs and explicit expiry/cleanup. |
| B-12 | Independent Gate B review has not occurred. | A reviewer must validate this map against the final schema and governing policy, then record scope-specific acceptance. |

## Canonical eligibility and evaluation boundary

Every board bootstrap, continuation read, and detail read must capture exactly one database evaluation
time using PostgreSQL transaction time inside one short read-only transaction. All expiry and delay
predicates use that captured value. Serialization, rendering, sanitization, route encoding, and browser
formatting happen after the immutable fact bundle is captured and must not keep the transaction open.

A task is eligible for the Stage 1 board/detail only when all of these are true in the active authority
generation and active registry:

1. `DishTask.existence_state <> 'retired'`;
2. `CurrentTaskCompletion.completed = false`;
3. one current placement exists with `registry_version_id` equal to the active registry and a non-null
   section present in that registry;
4. the current task project membership for the containing governed project has `is_member = true`;
5. the task authority head and current content activation/version are complete and belong to the same
   generation/task bundle.

`isolated` is not automatically equivalent to retired in the current schema. Whether an isolated task
is display-eligible is **unresolved** and must be decided from the governing isolation contract before
the eligibility predicate is accepted. Until then, Stage 3 must not silently include or exclude it.

Card order is deterministic `lower(current title), task_id` in the current read model. The contract
requires deterministic title ordering but forbids raw internal keys in the DTO. This ordering is a
reasonable implementation candidate, subject to acceptance of Unicode/collation behavior and an
explicit database collation expression. The technical task key remains cursor-internal only.

## Required frontend-owned read boundaries

The current general read model remains available to existing callers. The frontend should add narrow,
read-only owners rather than alter it into a browser DTO service:

| Proposed owner | Responsibility |
|---|---|
| `dish_pg/frontend_board_query.py` | One coherent bootstrap query, section continuation query, card fact aggregation, attention inputs, and internal snapshot/continuity inputs. |
| `dish_pg/frontend_detail_query.py` | One coherent eligible-task fact bundle containing canonical content and every disclosure/advisory/projection input. |
| `dish_pg/frontend_route_identity.py` | Bounded typed/environment-scoped route identities and legacy normalization; no raw UUID/GID exposure. |
| `dish_pg/frontend_cursor.py` | Bounded opaque tamper-resistant cursor lifecycle, compatibility checks, expiry, and optional handle cleanup. |
| `dish_service/frontend_attention.py` | Versioned accepted predicates, labels, severities, and deterministic registry order. |
| `dish_service/frontend_disclosure.py` | Versioned category/source registry and bounded factual detail formatting. |
| `dish_service/frontend_projection.py` | Versioned projection state reducer and human presentation, using captured durable facts plus one optional readiness sample. |
| `dish_service/frontend_advisory.py` | Non-authorizing factual next-step advisory derived from the same captured workflow facts as the authority layer. |
| `dish_service/frontend_renderer.py` | Pinned bounded renderer/sanitizer and inert fallback over captured canonical body source. |

Names are implementation proposals. Their separation and authority limits are required; exact file
names may change during implementation review.

## Board-bootstrap field map

| Browser/result field | Canonical source and join | Selection/evaluation/precedence | Required support and proof | Status |
|---|---|---|---|---|
| Active generation | `AuthorityGeneration` | Exactly one row with `status='active'`; none/multiple is service/configuration failure. | Reuse `active_generation()` semantics, add cardinality test. | Mapped |
| Active registry | `ActiveSectionRegistry` joined to `SectionRegistryVersion` | Exact row for active generation; registry version/revision captured once. | Bootstrap transaction invariant tests. | Mapped |
| Ordered sections | `SectionRegistryEntry` joined `GovernedSection` and `GovernedProject` | Entries for active registry ordered by `ordinal`; section/project lifecycle must be active. Every registry section is returned, including empty sections. | One bounded registry query; ambiguity validation. | Mapped, lifecycle filter to add |
| Section label | `SectionRegistryEntry.display_name` | Current active registry value. | Bounded length/normalization in DTO. | Mapped |
| Project label | `GovernedProject.logical_name` via `GovernedSection.project_id` | Emit only when equal normalized section labels need disambiguation; if normalized project+section still collides, fail `board_configuration_invalid`. | Frontend configuration validator and collision tests. | Mapped |
| Section route identity | Internal `GovernedSection.section_id` plus environment/type binding | Browser receives only current normalized route identity. | New route-identity service and wrong-type/environment tests. | Support required B-03 |
| Section continuity identity | Server-owned digest/handle over active generation/registry, section, normalized query contract, effective page sizes, every eligible card/order/visible fact and attention set in the section, and time-threshold crossings | Equality only has contract meaning; no raw revisions in DTO. | New continuity builder with deterministic fixtures and refresh tests. | Support required B-11 |
| Effective page size | Frontend deployment configuration | Positive bounded value, returned exactly. | Startup bounds and schema tests. | Support required |
| Card task identity | Internal `DishTask.task_id` | Current normalized browser route identity; one task at most once across accepted pages. | Route-identity service and duplicate detection. | Support required B-03 |
| Card title | `TaskAuthorityHead.current_content_activation_id` → `ContentActivation` → `ContentVersion.title` | Current active content version for same generation/task; nonblank. | Set-oriented join already demonstrated in `section_tasks()`. | Mapped |
| Card section identity | Current placement + containing registry entry | Must equal containing section route identity. | DTO invariant test. | Mapped |
| Eligibility | `DishTask`, `CurrentTaskCompletion`, `CurrentTaskSectionPlacement`, `CurrentTaskProjectMembership`, `TaskAuthorityHead`, registry/project/section | Non-retired, incomplete, active-registry placement, current membership true, complete authority bundle; isolation rule still unresolved. | Frontend eligibility CTE and equivalence tests. | Partially mapped |
| Active operation | `WorkflowOperation` | At most one `lifecycle='open'` row per generation/task by partial unique index. | Bulk outer join/CTE; invariant failure if cardinality is violated. | Mapped |
| Operation label | `WorkflowOperation.kind` through a checked-in closed presentation registry | Browser must not title-case arbitrary database text. | Versioned operation-label registry and schema sync. | Support required |
| Phase label | `WorkflowOperation.phase` through a checked-in closed/bounded presentation mapping | Optional approved display label; unknown required phase is contract failure or service-owned generic factual representation only if contract permits. | Phase registry/equivalence decision. | Unresolved presentation mapping |
| No-operation status | Absence of an open `WorkflowOperation` | Emit approved closed `no_active_operation` state. | Outer-join and schema tests. | Mapped |
| Attention codes | See registry below | Derived only from accepted named predicates at the same evaluation time, in fixed registry order, with no duplicates. | Bulk aggregate CTE plus versioned registry/equivalence tests. | Blocked B-04–B-09 |
| Per-response notices | Attention codes on only cards returned in that response | One contribution per distinct returned task/code; grouped counts happen over accepted loaded contributions in the client. | DTO notice builder and equivalence tests. | Mapped once predicates pass |
| `next_cursor` | Server cursor service over section/query/page boundary | Present exactly when an additional eligible row exists; bounded, opaque, tamper-resistant, retry-safe, expiring no later than session lifetime. | New cursor service; current `CursorCodec` is insufficient because it has no expiry/contract/compatibility/service-unavailable distinction. | Support required B-11 |
| Board snapshot identity | Server-owned digest/handle over the exact presentation inputs defined in `frontend-imp.md` | Equal only for equivalent active registry/order/labels, effective first-page size, first-page identities/order/visible fields, continuity identities, cursor presence, and notices. | New snapshot builder and deterministic change matrix. | Support required B-11 |

### Current read-model gap

`PostgresReadModel.section_tasks()` is not an approved Stage 3 data source by itself because it:

- issues one query for one section rather than one coherent all-section bootstrap;
- omits current-project-membership validation;
- does not filter `CurrentTaskCompletion.completed = false`;
- does not settle isolated-task eligibility;
- returns UUID and Asana alias values rather than frontend route identities;
- has no open-operation or attention aggregation;
- has no shared evaluation time, board snapshot, section continuity identity, notices, or contract
  capacity outcome;
- uses a stateless cursor without expiry or the required invalid/stale/unavailable distinctions.

The useful part to retain is its set-oriented content/placement/head/completion join and deterministic
keyset boundary pattern.

## Attention-code predicate registry

The following table is the Gate B decision surface. **Candidate** text is not authorization. Each row
must become an exact checked-in backend predicate with accepted tests before Stage 3.

| Code | Contract meaning | Current durable facts | Candidate exact predicate / precedence | Decision |
|---|---|---|---|---|
| `lease_attention` | Lease is expired, invalid, or contested; healthy active lease excluded. | `ServiceLease.state`, `expires_at`, operation/task links, one-active-actor partial index. | `expired` can be `state='active' AND expires_at <= evaluation_time` (and possibly durable `state='expired'` if that state remains presentation-relevant). No current column/relation names **invalid** or **contested**. Do not equate every non-active state with attention: `released` and `recovered` are ordinary terminal states. | **Unresolved B-04.** Add named durable/read-support predicates for invalid/contested or amend approved meaning. |
| `verification_attention` | Verification failed, disputed, or awaiting human review; ordinary pending/in-progress excluded. | `VerificationCycle.lifecycle/outcome`; `HumanReviewRequirement(route='human_review', state='open')`; workflow operation kind/lifecycle/phase. | Awaiting human review can map exactly to an open `HumanReviewRequirement` with `route='human_review'` linked to the current operation/cycle. Current lifecycle supports `rejected`, but the contract says failed; current `outcome` is free text and no dispute relation is named. | **Partially mapped; unresolved B-05** for failed/disputed and current-cycle precedence. |
| `hold_active` | A named active hold exists. | `EvidenceHold(state='open')`; `HumanReviewRequirement(route='two_pass_hold', state='open')`. | Candidate: existence of either open durable hold type linked to the current operation, preserving the hold kind for detail disclosure. The reviewer must confirm both are approved “named hold” authorities rather than only `EvidenceHold`. | **Candidate requires policy equivalence review.** |
| `recovery_required` | A named unresolved recovery requirement exists. | Recovery outcomes are computed in command/operation paths; abandonment states and projection uncertainty are separate facts. No single current task-scoped durable relation names an unresolved recovery requirement. | Do not infer from operation phase strings, failed operations, expired leases, or active abandonment. Introduce a task-scoped frontend read-support projection sourced transactionally from the governing recovery evidence, with explicit kind/state/opened/resolved facts, or amend the contract. | **Unresolved B-06.** |
| `abandonment_active` | An active abandonment fact exists. | `AbandonmentAttempt.state`; partial unique index already defines active set. | Exact candidate: an attempt for current generation/task with `state IN ('preparing','published','blocked','reconciling')`. The active partial unique index is the strongest current canonical definition. | **Mapped candidate; acceptance/equivalence required.** |
| `succession_active` | An active succession fact exists. | `OperationSuccessionEdge` plus `AbandonmentAttempt` and source/successor `WorkflowOperation`. | Candidate: a succession edge whose abandonment is currently `published`, whose successor matches `AbandonmentAttempt.successor_operation_id`, and whose successor operation remains `lifecycle='open'`. Whether succession remains active in another abandonment state is not stated. | **Candidate requires policy decision.** |
| `projection_abnormal` | Projection state is delayed, failed, drifted, unknown, or unavailable; current/not-configured excluded. | Active `ProjectionEpoch`, `TaskProjectionMapping`, `ProjectionOutboxEvent`, latest `ProjectionAttempt`/`Observation`/`Adjudication`, open `ProjectionDriftEvent`, reconciliation/readiness evidence. | Precedence candidate: `unavailable` when configured projection presentation cannot be established; `drifted` for open drift; `failed` for terminal blocked/not-applied state requiring intervention; `unknown` for uncertain evidence; `delayed` for unresolved pending/claimed/dispatched work older than configured threshold; `current` when mapped and no higher state; `not_configured` only when projection is intentionally absent. Exact threshold, latest-event selection, not-applied semantics, readiness source, and precedence require acceptance. | **Unresolved B-07.** |

### Attention test obligations

For every accepted predicate, tests must prove:

- positive, negative, boundary-time, and terminal-state cases;
- one-evaluation-time behavior;
- equivalence with the governing workflow/recovery/projection owner;
- no per-task Python loop or scalar query;
- stable registry order and no duplicates;
- matching detail disclosure/projection data for an open task;
- no attention from merely suggestive phase text, free-text reason, body `Status:` text, or browser
  inference;
- contract-version change when predicate, label, or severity changes.

## Continuation map

A continuation request resolves the section route identity, validates a cursor, then performs one
bounded coherent read at one evaluation time. The cursor must bind at least:

- environment and object type;
- active authority generation and registry identity/revision;
- current normalized section identity;
- exact eligibility predicate version and attention/presentation contract version;
- effective continuation page size;
- deterministic ordering/collation version;
- prior page boundary;
- section continuity identity or equivalent compatibility input;
- issued/expiry time and cursor representation version.

The response repeats the current normalized section and continuity identities, returns `1..page_size`
cards when nonterminal, and includes notices only for returned cards. A current accepted cursor cannot
produce an empty nonterminal page. Cursor-store outages map to `service_unavailable`; malformed or
wrong-scope tokens map to `cursor_invalid`; expired/retired/incompatible valid tokens map to
`cursor_stale`.

## Task-detail field map

| Browser/result field | Canonical source and join | Selection/evaluation/precedence | Required support and proof | Status |
|---|---|---|---|---|
| Task route identity | Internal `DishTask.task_id` | Normalize accepted legacy identity before committing visible state/history. | Shared route-identity service. | Support required B-03 |
| Eligibility | Same canonical predicate as board | Freshly evaluated; missing identity → `task_not_found`, known but completed/retired/out-of-registry → `task_ineligible`. | Detail eligibility query and race tests. | Partially mapped |
| Canonical title/body | Current authority head → activation → `ContentVersion.title/body` | One current version in same coherent snapshot; raw body remains server-side during normal rendering. | Set-oriented detail bundle. | Mapped |
| Project label | Current placement → governed section → governed project | Current logical label. | Join and bounded text tests. | Mapped |
| Section label | Current placement → active registry entry | Current active display label. | Join and registry-coherence test. | Mapped |
| Destination label | Governing current workflow destination fact | The current models do not expose one generic destination relation. It may be represented by operation-specific persisted workflow facts or policy-derived current snapshot; phase/body text is not authority. | Name exact canonical destination source per operation kind or add a frontend factual projection. | **Unresolved.** |
| Workflow status | Current open `WorkflowOperation` | Same status mapping as cards. | Shared status registry/equivalence. | Partially mapped |
| Lease disclosures | Accepted current/relevant `ServiceLease` facts | Approved owner/role, state label, and human-readable expiry only; stable backend order. Active attention must have at least one matching item. | Disclosure registry; redact raw owner if it is not an approved human label. | Predicate/presentation review required |
| Verification disclosures | Current relevant `VerificationCycle` and human-review fact | Approved state and summary only; identify latest/current cycle by exact operation/cycle precedence. | Disclosure registry and policy equivalence. | Partially mapped |
| Hold disclosures | Accepted open/relevant `EvidenceHold` and/or two-pass `HumanReviewRequirement` | Approved kind/state and summary, not raw question/reason unless explicitly approved and bounded. | Registry and source decision. | Candidate |
| Recovery disclosures | Named durable recovery support state | Must correspond to every `recovery_required` attention. | New support state/service. | Blocked B-06 |
| Abandonment disclosures | `AbandonmentAttempt` | Approved kind/state and summary; no raw IDs, request evidence, owner IDs, or executable continuation. | Registry and bounded formatter. | Mapped candidate |
| Succession disclosures | `OperationSuccessionEdge` plus accepted active predicate | Approved kind/state and summary; no raw source/successor IDs. | Registry and bounded formatter. | Candidate |
| Projection object | Accepted projection reducer inputs | Emit only abnormal state/message/optional observation time. Healthy and not-configured omitted. | Projection reducer B-07. | Blocked |
| Advisory code/message | Same immutable workflow/recovery facts captured for detail | Backend factual service only; stable non-sensitive code, workflow perspective, `invokable_by_frontend=false`; never serialize `legal_actions`. | New advisory service with equivalence corpus. | Support required |
| Rendered body | Captured `ContentVersion.body` | Pinned bounded renderer/sanitizer after transaction; normal branch only sanitized allowed HTML. | New renderer/sanitizer and security corpus. | Support required Stage 4 |
| Plain-text fallback | Same captured canonical body | Only when valid bounded content cannot be rendered safely; inserted as text; one task-targeted `render_rejected` notice. Capacity/dependency failures remain errors. | Failure taxonomy tests. | Support required Stage 4 |
| Detail notices | Accepted detail attention/projection/rendering contributions | Bounded closed registry; active attention must have matching disclosure/projection object. | DTO cross-field validator. | Depends on registries |

### Current `task_view()` gap

`PostgresReadModel.task_view()` must not be adapted by merely deleting fields from its serialized
result. It currently:

- accepts raw UUID/Asana task references instead of browser identities;
- does not enforce incomplete + active-registry + current-membership eligibility;
- calls `_workflow_snapshot()`, which performs several scalar queries and computes `legal_actions`;
- exposes operation IDs/revisions and technical content/revision identities in its internal object;
- has no disclosures, destination authority, advisory DTO, notices, renderer, or sanitizer;
- always reports projection as `not_configured`.

The frontend detail service must capture a dedicated immutable fact bundle and derive presentation
after closing the read transaction.

## Board query shape and bounded-work proof plan

The recommended bootstrap is one application-service call using a short read-only transaction and a
small fixed number of set-oriented SQL statements, independent of returned task count:

1. capture evaluation time, active generation, active registry, registry entries, project labels, and
   validate section-label/path uniqueness;
2. run one eligible-card query across all registry sections using a window function partitioned by
   section, fetching at most `first_page_size + 1` rows per section and joining the single open
   operation;
3. bulk aggregate accepted attention-source facts for only candidate returned task IDs, or integrate
   them as lateral/CTE aggregates in the same statement when query plans remain bounded;
4. construct DTOs, notices, continuity inputs, cursors, and snapshot identity from immutable rows.

An empty registry is a successful zero-section board. A configured section-count or response bound is
checked before expensive card work and maps to `board_capacity_exceeded`. Invalid labels/path
configuration maps to `board_configuration_invalid`.

Continuation uses one card query plus at most one bulk attention query. Detail uses one base task fact
query plus a fixed number of bulk/aggregate queries for the one task. No query count may grow with the
number of cards, disclosures, or sections.

Required proof before Gate B acceptance:

- representative `EXPLAIN (ANALYZE, BUFFERS)` on minimum, typical, and configured-maximum datasets;
- query-count assertion independent of section/card count;
- execution deadline and statement timeout behavior;
- response-size and serialization bound tests;
- cold/warm performance evidence in the PostgreSQL test lane;
- pagination retry and threshold-crossing fixtures;
- no Asana/network calls and no workflow-policy loop per card.

## Index/read-support plan to reconcile with migration

These are candidate frontend-owned support indexes. The migration owner must confirm existing primary,
unique, partial, and foreign-key indexes before adding duplicates.

- active-registry placement/eligibility path over
  `current_task_section_placements(generation_id, registry_version_id, section_id, task_id)`;
- incomplete lookup over `current_task_completion(generation_id, task_id)` with a partial predicate
  for `completed = false` if plans benefit;
- current project membership over
  `current_task_project_memberships(generation_id, project_id, task_id)` with `is_member = true`;
- title ordering support joining the authority head/current content activation; if a materialized
  frontend read projection is chosen, index its normalized title key and task ID rather than
  denormalizing canonical authority without a transition owner;
- open/current workflow facts by `(generation_id, task_id)`; the existing one-open-operation partial
  unique index may suffice;
- lease presentation over `(generation_id, task_id, state, expires_at)`;
- open human-review/hold facts by task and operation; existing uniqueness is operation-scoped, so
  task-scoped lookup plans need evidence;
- active abandonment by `(generation_id, task_id)` is already backed by its partial unique index;
- succession lookup by `task_id` plus joined active abandonment/successor lifecycle;
- projection event/drift lookup by `(generation_id, task_id, state, created_at)` and open drift by
  `(generation_id, task_id)`;
- any frontend-owned recovery-support table indexed by `(generation_id, task_id, state)` with one
  current unresolved fact per kind where governing semantics permit.

A materialized read projection is permitted only as frontend-owned support. It may cache facts but
cannot become task/workflow/completion/projection authority. Its refresh/transaction boundary and
failure mode must be explicit, and canonical-source equivalence remains tested.

## Snapshot, continuity, and presentation precedence

Internal identity inputs must use accepted canonical revisions/facts, not browser-visible raw values.
At minimum:

- board snapshot changes when any contract-listed first-page presentation input changes;
- section continuity changes when any pagination-relevant membership, eligibility, title/order,
  visible status, attention, page-size/query-contract input, or time-derived threshold anywhere in
  that section changes;
- detail derives all facts from one snapshot and does not expose its internal current-view token;
- projection presentation precedence is applied once by the backend reducer;
- attention registry order is fixed as:
  lease, Verification, hold, recovery, abandonment, succession, projection;
- disclosure order is fixed as:
  lease, Verification, hold, recovery, abandonment, succession;
- no generic authoritative `blocked` card field is emitted;
- notices are consequences of accepted registered predicates, not a second inference path.

## Migration reconciliation checklist

Before the Gate B reviewer can accept the Stage 3 scope, record:

- exact migration head and production-candidate schema revision;
- exact lifecycle/check-constraint values for every table named here;
- whether isolated tasks are board/detail eligible;
- accepted route-identity design and any supporting table/migration;
- accepted attention predicate registry, including every previously unresolved term;
- accepted projection reducer, delay threshold, readiness input, and precedence;
- accepted recovery durable/read-support source;
- active registry/project/section lifecycle behavior;
- required indexes and query-plan evidence;
- board snapshot, continuity, and cursor representation/lifetime;
- OpenAPI DTO synchronization and closed registry versions.

Before Stage 4, additionally record:

- exact destination source;
- disclosure category/source registry and stable ordering;
- advisory fact/service equivalence;
- renderer/sanitizer versions, bounds, and emitted allowlist;
- projection observation-time source;
- detail query plan and response/rendering capacity evidence.

## Test ownership matrix

| Test family | Required evidence |
|---|---|
| Predicate unit tests | Exact registry positive/negative/boundary cases and precedence. |
| Workflow equivalence | Accepted attention/disclosure/advisory results match governing workflow/recovery decisions for a shared corpus. |
| PostgreSQL integration | Coherent generation/registry/task joins, eligibility, empty sections, duplicates, movement, completion, retirement, isolation decision, and threshold times. |
| Query-bound tests | Fixed query count, no per-card loop, statement/response bounds, capacity errors. |
| Cursor tests | Tamper, wrong type/environment/section/page size, expiry, cleanup, stale state, unavailable validator, lost-response retry. |
| Identity tests | No raw UUID/GID leakage, legacy normalization, collision handling, object/environment scoping. |
| DTO/schema tests | Closed objects, registry synchronization, cross-field disclosure/attention requirements, unknown-code rejection. |
| Rendering tests | Approved corpus, raw HTML escaping, dangerous URL neutralization, fallback taxonomy, deterministic output. |
| Browser acceptance | Real density/order, Load more, deep links, moved/completed/retired tasks, projection/attention presentations, last-safe-view behavior. |

## Gate B pass conditions

The Stage 3 portion may pass only when:

- B-01 through B-12 are resolved for board scope;
- every attention term has one accepted named predicate with no guessed semantics;
- the final migrated schema and indexes are recorded;
- the board query and cursor designs have bounded-work evidence;
- the independent reviewer records an exact commit/build/schema revision and pass.

The Stage 4 portion may pass only after the same reviewer process covers the detail map, including the
previously unresolved destination, disclosure, advisory, rendering, and projection facts.
