# Frontend Gate B canonical-data source map

## Status

**Authoring map complete for the Stage 3 board and Stage 4 detail scope; Gate B is not passed.**

This packet maps the approved frontend fields against the current PostgreSQL models, read surfaces,
and frontend contracts. It is reconciled to checked-in Alembic head
`0037_release_identity_contract`. PostgreSQL remains a non-authoritative dark-launch target: the
frontend read core may be implemented and exercised locally against it without transferring authority.
Gate B still must pass before the Stage 3 production/private HTTP/browser surface is activated. A
read-only Stage 4 detail/deep-link candidate may be exercised only through the same explicit
loopback local observation boundary; the Stage 4 portion still requires independent acceptance before
production/private activation.

No predicate marked **unresolved** below may be guessed in a query, browser component, label mapper,
or DTO builder. The Stage 3 implementation therefore emits only the durable subset currently mapped;
unresolved invalid/contested lease and failed/disputed Verification meanings remain absent rather
than inferred.

## Evidence inspected

- Product and implementation contracts: `frontend.md`, `frontend-imp.md`.
- PostgreSQL authority models: `dish_pg/models.py`, `dish_pg/stage3_models.py`,
  `dish_pg/stage5_models.py`, and `dish_pg/stage6_models.py`.
- Current PostgreSQL read surfaces: `dish_pg/read_model.py`, the Stage 3 candidate
  `dish_pg/frontend_board_query.py`, and the Stage 4 local candidates
  `dish_pg/frontend_detail_query.py` / `dish_pg/frontend_projection_query.py`.
- PostgreSQL transition and projection code under `dish_pg/`.
- Migration revisions `0033_frontend_security` through `0037_release_identity_contract`; revisions
  after `0033` add repair, persistence-integrity, exact-run-revocation, and release-identity rules but
  no new frontend-owned support tables.
- Architecture entrypoint `docs/architecture/index.md` and the routed PostgreSQL, authority, package, and testing-boundary documents under `docs/architecture/`, plus operational migration/testing documents in `docs/`.
- Workflow-policy and recovery implementation under `dish_tool/`, used only to identify current
  authority concepts; those Python paths are not approved as per-card query loops.
- Current frontend DTO shapes, presentation registries, fixtures, OpenAPI document, and browser suite.

The current checked-in models are treated as design evidence, not proof that the same schema is live in
production. This map is reconciled to checked-in head `0037_release_identity_contract`; final
production rollout reconciliation and independent Gate B review remain mandatory before production/private
HTTP/browser activation.

## Material findings blocking Gate B

| ID | Finding | Required resolution |
|---|---|---|
| B-01 | The map is reconciled to checked-in Alembic head `0037_release_identity_contract`; frontend support remains limited to the tables introduced by `0033_frontend_security`, while PostgreSQL is a non-authoritative dark-launch target rather than production authority. | Reconcile again to the exact deployed schema/runtime evidence before production/private HTTP/browser activation; authority transfer is not required for read-only use. |
| B-02 | The set-oriented board query in `dish_pg/frontend_board_query.py` is integrated behind disabled private PostgreSQL-read activation and the loopback observation harness; it lacks accepted native PostgreSQL plan/isolation evidence. | Review the query against native PostgreSQL, record bounded plans/isolation, and keep it read-only/no-network. |
| B-03 | Stateless typed/environment-scoped route identities now exist in `dish_service/frontend_tokens.py`; secret lifetime/rotation is not yet accepted. | Review secret lifecycle, collision/bounds evidence, and deployment ownership before HTTP activation. |
| B-04 | The English terms **invalid lease** and **contested lease** still have no exact named PostgreSQL predicate. The candidate query emits only durable expired-lease attention for the latest actor attempt on the current open operation. | Name durable predicates or amend the approved meaning; do not broaden non-active lease states heuristically. |
| B-05 | Verification **failed** and **disputed** remain unresolved. The candidate query emits only durable open human-review attention. | Name exact failed/disputed/current-cycle predicates and add policy-equivalence tests. |
| B-06 | A task-scoped recovery candidate is now mapped as `CommandExecution.status='uncertain'` without a corresponding `RequestUncertaintyResolution`; equivalence with governing recovery semantics is not yet accepted. | Review and accept/reject that mapping before HTTP activation; no new support table is currently required. |
| B-07 | The candidate query can flag open drift, live-origin blocked/uncertain outbox work, and explicitly configured delayed live-origin work, but the full projection presentation reducer and precedence remain unaccepted. | Accept the reducer, delay threshold source, readiness input, and precedence before exposing final projection presentation. |
| B-08 | The current `task_view()` remains unsuitable for Stage 4 browser detail. A dedicated local candidate now captures a bounded immutable detail fact bundle in `dish_pg/frontend_detail_query.py` and derives the browser DTO in `dish_service/frontend_detail.py`; this has not been accepted for production/private activation. | Review the candidate against native PostgreSQL, current eligibility/workflow policy, and response bounds; do not serialize `TaskCurrentView`. |
| B-09 | Versioned board/detail presentation registries and local candidate disclosure/advisory/projection/rendering services now exist. Their policy equivalence, destination source, projection reducer acceptance, and normalization/collation equivalence remain unresolved. | Review and accept the detail registries/services against the governing policy and native runtime before production/private activation. |
| B-10 | Focused tests prove a fixed three-statement bootstrap and native PostgreSQL smoke tests prove board/detail reads, but representative `EXPLAIN`, transaction-isolation/coherence, response-size, and execution-time evidence remain outstanding. | Record native PostgreSQL bounded-work evidence and enforce the chosen short coherent read transaction before HTTP activation. |
| B-11 | Stateless retry-safe cursor, section continuity, and board snapshot candidates now exist in `dish_service/frontend_tokens.py` and `dish_service/frontend_board.py`; they deliberately do not promise a frozen task snapshot. | Review token secret lifecycle, expiry, compatibility semantics, and keyset boundary behavior before HTTP activation. |
| B-12 | Independent Gate B review has not occurred. | A reviewer must validate this map against the deployed dark-launch schema/runtime and governing policy, then record scope-specific acceptance. |

## Canonical eligibility and evaluation boundary

Every board bootstrap, continuation read, and detail read must capture exactly one database evaluation
time using PostgreSQL transaction time inside one short read-only transaction. All expiry and delay
predicates use that captured value. Serialization, rendering, sanitization, route encoding, and browser
formatting happen after the immutable fact bundle is captured and must not keep the transaction open.

A task is eligible for the Stage 1 board/detail only when all of these are true in the active authority
generation and active registry:

1. `DishTask.existence_state IN ('ordinary', 'isolated')`; isolated rows remain visible and are marked `ISOLATED`;
2. `CurrentTaskCompletion.completed = false`;
3. one current placement exists with `registry_version_id` equal to the active registry and a non-null
   section present in that registry;
4. the current task project membership for the containing governed project has `is_member = true`;
5. the task authority head and current content activation/version are complete and belong to the same
   generation/task bundle.

`isolated` is an explicit PostgreSQL presentation state. It is display-eligible for the board and
detail read surface, remains visible under default filters, and contributes the first fixed
`isolated`/`ISOLATED` attention code. Migrated authoritative-source tasks must not be inferred isolated
without a separate accepted reconciliation rule.

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
| `dish_pg/frontend_detail_query.py` | One coherent eligible-task fact bundle containing canonical content and disclosure/advisory inputs for the local Stage 4 candidate. |
| `dish_pg/frontend_projection_query.py` | Bounded abnormal-projection fact capture used by the local Stage 4 candidate; final reducer/threshold semantics remain B-07-gated. |
| `dish_service/frontend_tokens.py` | Canonical stored Dish UUID task routes, typed/environment-scoped opaque section routes, opaque digests, and retry-safe expiring cursor tokens; no Asana GID exposure. |
| `dish_service/frontend_contract.py` | Versioned Stage 3 operation/phase/attention labels, severities, normalization candidate, and deterministic registry order. |
| `dish_service/frontend_board.py` | Closed board DTO builder, capacity/configuration validation, notices, snapshot/continuity identities, and stateless cursor lifecycle. |
| `dish_service/frontend_disclosure.py` | Versioned category/source registry and bounded factual detail formatting. |
| `dish_service/frontend_projection.py` | Versioned projection state reducer and human presentation, using captured durable facts plus one optional readiness sample. |
| `dish_service/frontend_advisory.py` | Non-authorizing factual next-step advisory derived from the same captured workflow facts as the authority layer. |
| `dish_service/frontend_renderer.py` | Pinned bounded renderer/sanitizer and inert fallback over captured canonical body source. |
| `dish_service/frontend_detail.py` | Closed local Stage 4 detail DTO builder, bounded opaque-route resolution, rendering/disclosure/advisory/projection composition, and cross-field validation. |

The Stage 3 owners and the listed Stage 4 read-only owners are checked-in candidate implementations.
They remain non-authoritative and do not constitute production/private activation or Gate B acceptance;
their separation and authority limits are required.

## Board-bootstrap field map

| Browser/result field | Canonical source and join | Selection/evaluation/precedence | Required support and proof | Status |
|---|---|---|---|---|
| Active generation | `AuthorityGeneration` | Exactly one row with `status='active'`; none/multiple is service/configuration failure. | Reuse `active_generation()` semantics, add cardinality test. | Mapped |
| Active registry | `ActiveSectionRegistry` joined to `SectionRegistryVersion` | Exact row for active generation; registry version/revision captured once. | Bootstrap transaction invariant tests. | Mapped |
| Ordered sections | `SectionRegistryEntry` joined `GovernedSection` and `GovernedProject` | Entries for active registry ordered by `ordinal`; section/project lifecycle must be active. Every registry section is returned, including empty sections. | One bounded registry query; ambiguity validation. | Implemented candidate |
| Section label | `SectionRegistryEntry.display_name` | Current active registry value. | Bounded length/normalization in DTO. | Mapped |
| Project label | `GovernedProject.logical_name` via `GovernedSection.project_id` | Emit only when equal normalized section labels need disambiguation; if normalized project+section still collides, fail `board_configuration_invalid`. | Frontend configuration validator and collision tests. | Mapped |
| Section route identity | Internal `GovernedSection.section_id` plus environment/type binding | Browser receives only typed environment-scoped route identity. | `frontend_tokens.py` and wrong-type/environment tests. | Implemented candidate B-03 |
| Section continuity identity | Server-owned digest over active generation/registry, section, query/normalization contract versions, and effective page sizes | Equality means pagination-contract compatibility, not a frozen task snapshot; ordinary keyset boundary movement between requests is acceptable. | `frontend_board.py` deterministic digest and cursor tests. | Implemented candidate B-11 |
| Effective page size | Frontend deployment configuration | Positive bounded value, returned exactly. | Startup bounds and schema tests. | Support required |
| Card task identity | Internal `DishTask.task_id` | Typed environment-scoped browser route identity; immutable task ID remains cursor-internal only. | `frontend_tokens.py` and pagination identity tests. | Implemented candidate B-03 |
| Card title | `TaskAuthorityHead.current_content_activation_id` → `ContentActivation` → `ContentVersion.title` | Current active content version for same generation/task; nonblank. | Set-oriented join already demonstrated in `section_tasks()`. | Mapped |
| Card section identity | Current placement + containing registry entry | Must equal containing section route identity. | DTO invariant test. | Mapped |
| Eligibility | `DishTask`, `CurrentTaskCompletion`, `CurrentTaskSectionPlacement`, `CurrentTaskProjectMembership`, `TaskAuthorityHead`, registry/project/section | `ordinary` or `isolated`, incomplete, active-registry placement, current membership true, complete authority bundle; isolated remains visible/marked. | Set-oriented frontend eligibility query and tests. | Implemented candidate |
| Active operation | `WorkflowOperation` | At most one `lifecycle='open'` row per generation/task by partial unique index. | Bulk outer join/CTE; invariant failure if cardinality is violated. | Mapped |
| Operation label | `WorkflowOperation.kind` through `frontend_contract.py` | Browser must not title-case arbitrary database text. | Closed versioned registry and schema sync. | Implemented candidate B-09 |
| Phase label | `WorkflowOperation.phase` through `frontend_contract.py` | Known Stage 3 phases map through a closed registry; unknown values fail closed. | Registry coverage/equivalence review. | Implemented candidate B-09; coverage review pending |
| No-operation status | Absence of an open `WorkflowOperation` | Emit approved closed `no_active_operation` state. | Outer-join and schema tests. | Mapped |
| Attention codes | See registry below | Derived only from currently mapped durable predicates at the same evaluation time, in fixed registry order, with no duplicates. Unresolved predicate branches are omitted. | Set-oriented query plus versioned registry/equivalence tests. | Candidate; B-04/B-05/B-06/B-07 review remains |
| Per-response notices | Attention codes on only cards returned in that response | One contribution per distinct returned task/code; grouped counts happen over accepted loaded contributions in the client. | DTO notice builder and equivalence tests. | Mapped once predicates pass |
| `next_cursor` | Stateless sealed cursor over section/query/page boundary | Present exactly when an additional eligible row exists; bounded, opaque, tamper-evident, retry-safe, and expiring. | `frontend_tokens.py`; deployment secret lifetime/rotation review remains. | Implemented candidate B-11 |
| Board snapshot identity | Server-owned digest over exact returned first-page presentation inputs | Equal only for equivalent returned Stage 3 bootstrap presentation under the current contract versions. | `frontend_board.py` digest and deterministic tests. | Implemented candidate B-11 |

### Current read-model gap

`PostgresReadModel.section_tasks()` is not an approved Stage 3 data source by itself because it:

- issues one query for one section rather than one coherent all-section bootstrap;
- omits current-project-membership validation;
- does not filter `CurrentTaskCompletion.completed = false`;
- predates the accepted isolated-task visibility rule;
- returns UUID and Asana alias values rather than frontend route identities;
- has no open-operation or attention aggregation;
- has no shared evaluation time, board snapshot, section continuity identity, notices, or contract
  capacity outcome;
- exposes a generic cursor shape rather than the frontend-specific contract/expiry/compatibility token.

The useful part to retain is its set-oriented content/placement/head/completion join and deterministic
keyset boundary pattern.

## Attention-code predicate registry

The following table is the Gate B decision surface. **Candidate** text is not authorization. Each row
must become an exact checked-in backend predicate with accepted tests before Stage 3.

| Code | Contract meaning | Current durable facts | Candidate exact predicate / precedence | Decision |
|---|---|---|---|---|
| `isolated` | Task is explicitly isolated but remains visible. | `DishTask.existence_state`. | Exact implemented predicate: `existence_state='isolated'`; it is first in registry order and renders `ISOLATED`. | **Accepted product decision; implemented candidate.** |
| `lease_attention` | Lease is expired, invalid, or contested; healthy active lease excluded. | `WorkflowOperation.lifecycle`; `ServiceLease.operation_id`, `actor_attempt_sequence`, `state`, `expires_at`; one-open-operation and one-active-actor partial indexes. | For the one current open operation, select the actor lease with the greatest `actor_attempt_sequence`. That relevant attempt qualifies when `state='expired'` or when `state='active' AND expires_at <= evaluation_time`. Any later actor attempt supersedes an earlier expired/released/recovered attempt for presentation, so historical terminal rows cannot create sticky attention. `released` and `recovered` do not themselves qualify. No current column/relation names **invalid** or **contested**. | **Partially mapped; unresolved B-04** for invalid/contested only. |
| `verification_attention` | Verification failed, disputed, or awaiting human review; ordinary pending/in-progress excluded. | `VerificationCycle.lifecycle/outcome`; `HumanReviewRequirement(route='human_review', state='open')`; workflow operation kind/lifecycle/phase. | Awaiting human review can map exactly to an open `HumanReviewRequirement` with `route='human_review'` linked to the current operation/cycle. Current lifecycle supports `rejected`, but the contract says failed; current `outcome` is free text and no dispute relation is named. | **Partially mapped; unresolved B-05** for failed/disputed and current-cycle precedence. |
| `hold_active` | A named active hold exists. | `EvidenceHold(state='open')`; `HumanReviewRequirement(route='two_pass_hold', state='open')`. | Candidate: existence of either open durable hold type linked to the current operation, preserving the hold kind for detail disclosure. The reviewer must confirm both are approved “named hold” authorities rather than only `EvidenceHold`. | **Candidate requires policy equivalence review.** |
| `recovery_required` | A named unresolved recovery requirement exists. | `CommandExecution.status`; `RequestUncertaintyResolution` keyed by request identity. | Candidate exact predicate: task-scoped `CommandExecution.status='uncertain'` with no matching uncertainty resolution. Do not infer from phase strings, failed operations, expired leases, or active abandonment. | **Mapped candidate; B-06 equivalence review required.** |
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

The response repeats the current section and continuity identities, returns `1..page_size` cards when
nonterminal, and includes notices only for returned cards. A current accepted cursor cannot produce an
empty nonterminal page. The current candidate is stateless, so there is no cursor-store availability
dependency. Malformed/tampered/wrong-scope tokens map to `cursor_invalid`; expired or incompatible
valid tokens map to `cursor_stale`.

## Task-detail field map

| Browser/result field | Canonical source and join | Selection/evaluation/precedence | Required support and proof | Status |
|---|---|---|---|---|
| Task route identity | Internal `DishTask.task_id` | Normalize accepted legacy identity before committing visible state/history. | Shared route-identity service. | Support required B-03 |
| Eligibility | Same canonical predicate as board | Freshly evaluated; missing identity → `task_not_found`, known but completed/retired/out-of-registry → `task_ineligible`. | Local detail eligibility query and focused race/eligibility tests; native-runtime acceptance pending. | Implemented local candidate |
| Canonical title/body | Current authority head → activation → `ContentVersion.title/body` | One current version in the local coherent read; raw body remains server-side during normal rendering. | Dedicated detail fact bundle plus rendering tests. | Implemented local candidate |
| Project label | Current placement → governed section → governed project | Current logical label. | Dedicated detail join and bounded response tests. | Implemented local candidate |
| Section label | Current placement → active registry entry | Current active display label. | Dedicated detail join and registry-coherence tests. | Implemented local candidate |
| Destination label | Governing current workflow destination fact | The current models do not expose one generic destination relation. It may be represented by operation-specific persisted workflow facts or policy-derived current snapshot; phase/body text is not authority. | Name exact canonical destination source per operation kind or add a frontend factual projection. | **Unresolved.** |
| Workflow status | Current open `WorkflowOperation` | Same status mapping as cards. | Shared closed status registry; policy-equivalence acceptance pending. | Implemented local candidate B-09 |
| Lease disclosures | Accepted current/relevant `ServiceLease` facts | Local candidate reuses the exact Stage 3 latest-actor-attempt/current-open-operation predicate and emits role/state/expiry without raw owner identity. Active attention must have a matching item. | Disclosure registry and policy-equivalence review. | Implemented local candidate; B-04 remains unresolved |
| Verification disclosures | Current relevant `VerificationCycle` and open human-review fact | Local candidate emits bounded lifecycle/outcome-presence facts or a generic awaiting-human-review disclosure; it does not invent failed/disputed semantics. | Disclosure registry and policy equivalence. | Implemented local candidate; B-05 remains unresolved |
| Hold disclosures | Accepted open/relevant `EvidenceHold` and/or two-pass `HumanReviewRequirement` | Local candidate emits bounded kind/state summaries and no raw question/reason. | Registry/source-equivalence review. | Implemented local candidate |
| Recovery disclosures | Current B-06 candidate: task-scoped uncertain `CommandExecution` without a recorded `RequestUncertaintyResolution` | Local candidate emits only a bounded factual summary and matches every current `recovery_required` attention. | Accept/reject B-06 equivalence. | Implemented local candidate; blocked for acceptance by B-06 |
| Abandonment disclosures | `AbandonmentAttempt` | Local candidate emits bounded state only; no raw IDs, request evidence, owner IDs, or executable continuation. | Registry/policy-equivalence review. | Implemented local candidate |
| Succession disclosures | `OperationSuccessionEdge` plus current active predicate | Local candidate emits a bounded active-succession fact; no raw source/successor IDs. | Registry/policy-equivalence review. | Implemented local candidate |
| Projection object | Current B-07 candidate inputs from drift and live-origin outbox facts | Local reducer emits only `drifted`, `failed`, `unknown`, or configured `delayed`; healthy/not-present is omitted. | Accept reducer, threshold source, readiness input, and precedence under B-07. | Implemented local candidate; blocked for acceptance by B-07 |
| Advisory code/message | Same immutable workflow facts captured for detail | Local backend factual service emits a closed non-sensitive code/message, workflow perspective, `invokable_by_frontend=false`; never serializes `legal_actions`. | Policy-equivalence corpus/review. | Implemented local candidate B-09 |
| Rendered body | Captured `ContentVersion.body` | Local bounded renderer runs after the read transaction; all source text is escaped and only its closed generated subset becomes `sanitized_html`. | Security corpus and production acceptance. | Implemented local candidate B-09 |
| Plain-text fallback | Same captured canonical body | Render rejection falls back to literal text plus one task-targeted `render_rejected` notice; capacity failure remains an error. | Failure taxonomy tests and production acceptance. | Implemented local candidate B-09 |
| Detail notices | Current detail attention/projection/rendering contributions | Closed bounded local registry; active attention must have matching disclosure and projection attention must have a projection object. | DTO cross-field validator and policy-equivalence review. | Implemented local candidate B-09 |

### Current `task_view()` gap

`PostgresReadModel.task_view()` must not be adapted by merely deleting fields from its serialized
result. It currently:

- accepts canonical stored Dish UUIDs directly; Asana task references remain outside the browser identity contract;
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

1. capture database evaluation time, active generation, and active registry context;
2. load bounded active-registry section/project metadata in ordinal order;
3. validate the configured section-count bound and all registry-fatal lifecycle, label,
   normalization/path, and route-identity conditions from that cheap metadata read;
4. run one eligible-card query across all registry sections using a window function partitioned by
   section, fetching at most `first_page_size + 1` rows per section and integrating the currently
   mapped attention facts set-wise;
5. construct DTOs, notices, continuity inputs, stateless cursors, and
   snapshot identity from the captured facts.

An empty registry is a successful zero-section board. The configured section-count bound and cheap
registry/configuration-fatal conditions are checked before expensive card work. Section-capacity
failure maps to `board_capacity_exceeded`; invalid lifecycle, labels, normalized paths, or route
identity configuration maps to `board_configuration_invalid`. Remaining response-size/query-work
bounds still require the explicit Gate B/native-runtime evidence listed below rather than an invented
threshold in the read core.

The current continuation candidate uses one context query plus one bounded card/attention query. Detail uses one base task fact
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
- section continuity binds active generation/registry, section identity, contract versions, and page
  sizes; it deliberately does not freeze all task facts between pages, so ordinary keyset boundary
  movement remains possible;
- detail derives all facts from one snapshot and does not expose its internal current-view token;
- projection presentation precedence is applied once by the backend reducer;
- attention registry order is fixed as:
  isolated, lease, Verification, hold, recovery, abandonment, succession, projection;
- disclosure order is fixed as:
  lease, Verification, hold, recovery, abandonment, succession;
- no generic authoritative `blocked` card field is emitted;
- notices are consequences of accepted registered predicates, not a second inference path.

## Migration reconciliation checklist

Before the Gate B reviewer can accept the Stage 3 scope, record:

- exact migration head and production-candidate schema revision;
- exact lifecycle/check-constraint values for every table named here;
- isolated-task visibility decision (`ordinary` and `isolated` are eligible; isolated is explicitly marked);
- accepted stateless route-identity design and deployment secret lifecycle;
- accepted attention predicate registry, including every previously unresolved term;
- accepted projection reducer, delay threshold, readiness input, and precedence;
- accepted/rejected recovery mapping from unresolved task-scoped command uncertainty;
- active registry/project/section lifecycle behavior;
- required indexes and query-plan evidence;
- board snapshot, non-frozen continuity semantics, stateless cursor representation/lifetime, and secret rotation;
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
| Identity tests | Canonical Dish UUID task routing, no Asana GID leakage, legacy-route rejection/normalization as applicable, and section object/environment scoping. |
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

## Acceptance handoff

Production/private HTTP/browser activation remains blocked on Gate B. Stable acceptance identifiers
are maintained in `../frontend/contracts/stage3-acceptance-cases.json`; implementation and unit
evidence does not count as acceptance until it runs against the exact reviewed schema/runtime and the
independent decision is recorded in `frontend-gate-b-review.md`.
