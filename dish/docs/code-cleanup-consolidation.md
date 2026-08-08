# Dish code-cleanup consolidation

**Status:** active engineering program. Consolidate overlapping command, workflow, replay, lease, effect, persistence, package, and test architecture without changing settled product, authority, halal, cutover, or operational rules.

This document is the maintained plan for that consolidation work. It replaces ad hoc "Stage B" references — that name collides with other uses elsewhere in `dish/docs` and is not used here. Internal workstream codes below (CC1–CC7) may be used for cross-references within this document and `docs/code-cleanup-maintainability.md`; anywhere else, use the descriptive workstream name.

See `docs/postgresql-cutover.md` §6.5 for the retention-class framework this plan's persistence work (CC5) uses, and §11 for how this program sequences relative to cutover.

## 1. Objective

Replace overlapping implementations with clear ownership while preserving observable behavior and safety invariants.

The target is not fewer lines, files, tables, or abstractions for their own sake. The target is:

- one production owner for each important rule;
- fewer parallel implementations of the same transition or classification;
- lower change fan-out;
- explicit dependency direction;
- locally understandable command, workflow, replay, lease, effect, and persistence behavior;
- tests that prove the boundary they claim;
- a development path that reaches a valid test result quickly and predictably;
- no loss of independent test oracles merely because production metadata has been consolidated.

This is a structural program, not a product-policy redesign.

## 2. Non-negotiable boundaries

This work must preserve the settled system guarantees, including:

- PostgreSQL remains non-authoritative until explicit cutover;
- Asana/SQLite authority and projection rules remain as currently approved until cutover changes them;
- request IDs remain permanently reserved and replay-safe;
- exact semantic-input binding and first authoritative outcome replay remain intact;
- stale owners, leases, generations, and replaced runs remain fenced;
- safe reclaim remains fail-closed and mechanically justified;
- Human Review remains asynchronous and Marco-only where a real Marco decision is required;
- dismissal of an invalid Human Review escalation is not a fabricated Marco decision or governed authorization;
- proposal approval and application remain separate where the product contract requires them;
- external-effect uncertainty remains explicit rather than guessed away;
- rollback and immutable audit history remain;
- GPT Actions remain supported and retain an independent Action-surface oracle;
- shadow/dark-launch origin remains strictly separated from live origin;
- cleanup does not silently change deployment topology, PostgreSQL admission, writer authority, or cutover policy.

Independent test oracles may intentionally duplicate expected behavior. Consolidating production ownership is not permission to derive every test expectation from production code.

## 3. Coordination rule

Do not fan multiple agents into shared workflow/authority/recovery/effect core while other in-flight recovery/authority work is still moving those seams. Parallelize only work that is genuinely non-overlapping, or discovery/proof work that does not mutate the same core files.

## 4. Workstreams

### CC1. Command and transport consolidation

#### Goal

Finish the single production command catalogue and make transports consume it without turning the catalogue into a workflow DSL.

#### Work

- keep one canonical production definition for command identity and stable command metadata;
- derive CLI enum/value metadata from canonical definitions while preserving surface-specific ordering/help behavior where intentional;
- derive OpenAPI command metadata without introducing package-import cycles;
- derive request-ID classification and shadow treatment from the appropriate canonical owner where those are shared facts;
- keep parsing, presentation, help text, HTTP rendering, and surface UX local when they are genuinely surface-specific;
- preserve independent exact GPT Action-surface tests;
- remove synchronization tests whose only purpose is comparing duplicate production registries after the duplicate registry is gone;
- do not add convenience APIs until multiple real consumers justify them.

#### Closure package

Perform one bounded CC1 tail pass over remaining command consumers/adapters: service/HTTP command routing, connected Action exposure, admin command metadata, request-ID classification, shadow/capture classification, generated clients where applicable. Remove duplicated command facts without moving workflow semantics into transport code.

### CC2. Workflow and authority consolidation

#### Goal

Create one clear application/domain owner for legal transitions and authoritative outcomes.

#### Required families

Consolidate, in bounded vertical slices:

- creation and ordinary workflow movement;
- verification and inspection;
- findings and research transitions;
- semantic proposals, approval, and application;
- Human Review and Marco override;
- rollback;
- operation acquisition, renewal, termination, reclaim, and succession;
- legal next actions.

Each owned operation should make it possible to reason in one place about: canonical state changes, authoritative outcome, audit facts, durable external-effect intents, and next legal actions.

Legacy, service, PostgreSQL, CLI, admin, and tests should call or derive from this owner instead of restating policy.

#### Timing

Do not begin broad CC2 consolidation while active recovery/Human Review workflow work is still integrating and being exercised. The first CC2 package should be one transition family, not a repo-wide rewrite.

### CC3. Request, replay, lease, and operation consolidation

#### Goal

Create one coherent model for request identity, replay, operation ownership, leases, fencing, reclaim, and recovery generation boundaries.

#### Required invariants

- permanent request-ID reservation;
- exact semantic-input binding;
- first authoritative outcome replay;
- explicit pending/uncertain execution state;
- one operation-ownership model;
- monotonic lease/fencing semantics;
- stale-owner rejection;
- mechanically safe reclaim only when the full predicate holds;
- linked successor lineage;
- recovery generation boundaries;
- fail-closed behavior when mutable eligibility facts drift.

#### Safe-reclaim integration rule

A reclaim path must not consume the source operation based on stale pre-transaction eligibility. Mutable database-owned eligibility must be revalidated under the writer transaction. Live external identity/placement must be checked as late as the protocol can safely support; unavoidable external-read-to-database-commit races must be documented and later successor claims must remain fail-closed.

### CC4. External-effect consolidation

#### Goal

Converge legacy direct-effect recovery and PostgreSQL projection/reconciliation into one understandable lifecycle without inventing an abstract universal effects framework.

Use the lifecycle: durable intent → owned attempt → external call → authoritative observation → confirmed/not-applied/uncertain settlement → reconciliation and drift handling.

Requirements:

- live and shadow origin must remain separate;
- uncertainty must be durable and inspectable;
- retries must respect request/effect identity;
- reconciliation must not silently convert unknown state into success;
- keep explicit Asana-specific code where that improves local reasoning.

### CC5. Persistence and schema simplification

#### Goal

Review persistent state by invariant, authority, consumer, and lifecycle rather than table-count targets.

For each candidate concept/table/module: state the invariant it enforces; identify every real production writer; identify every real production reader; classify the state as authoritative, historical, derived, cached, temporary, evidentiary, or transitional; identify recovery/forensic consumers separately from ordinary runtime consumers; decide retain, merge, archive, externalize, or remove; only then change schema/code.

The large table/migration count is evidence to investigate, not an instruction to collapse normalization.

#### Cutover-control-plane rule

The existing heavy PostgreSQL migration/cutover control plane remains the implemented operational contract unless an approved CC5 or cutover Phase 0 simplification replaces part of it (`docs/postgresql-cutover.md` §6.2, §11).

Do **not** freeze all evidentiary machinery until after cutover. CC5 may simplify it before cutover when dependency/lifecycle analysis proves that safe.

Use the retention classes defined in `docs/postgresql-cutover.md` §6.5 (permanent live invariant / explicitly retained transition history / cutover-stabilization evidence / one-shot implementation-tooling). The procedure/tooling for an irreversible event may be temporary while the durable fact that the boundary was crossed remains permanent history.

#### Dependency-proof package

Before destructive schema work, an independent agent may perform a bounded dependency proof for named candidate evidentiary/migration concepts and return an implementation-ready retain/remove/collapse decision. Do not delete from discovery alone.

If the retained schema is materially simplified before cutover, deliberately decide whether to: preserve selected shadow evidence; rebuild/reseed the dark-launch target; replace the active migration chain with a clean baseline; establish a new observation baseline before dark launch resumes. That is a code-quality/persistence decision, not itself the production authority cutover.

### CC6. Package and dependency boundaries

#### Target direction

1. domain types and invariants;
2. application operations and authority;
3. PostgreSQL persistence and transactions;
4. Asana projection/reconciliation;
5. transport and presentation;
6. temporary operational tools.

#### Rules

- domain/application code must not depend on CLI, HTTP, deployment, or tests;
- PostgreSQL must not import legacy SQLite workflow as its domain owner;
- transport must not implement workflow policy;
- operational scripts must not recreate product transitions;
- legacy persistence becomes an adapter during its remaining lifetime;
- package-import convenience must not recreate cycles.

#### Narrow package

Choose one demonstrable non-semantic dependency-direction violation outside the moving recovery/workflow core and fix that family only. Avoid recovery, leases, Human Review, workflow policy, and effects until their active changes settle.

### CC7. Test architecture consolidation

#### Goal

Make time-to-first-valid-result, evidence clarity, and repeatability first-class architecture concerns while preserving strong native/process/concurrency/recovery evidence.

#### Required properties

- one obvious supported serial bootstrap path;
- archive-based review must not assume a relocated `.venv` is runnable merely because files exist;
- optional acceleration dependencies must not contaminate the authoritative serial environment;
- offline/wheelhouse paths are documented where package-index completeness cannot be assumed;
- planner output should lead directly to runnable commands;
- small, explicit named lanes;
- native PostgreSQL, process, concurrency, lease/recovery, migration, and shared-state evidence remains serial unless isolation is actually proven;
- xdist is used only for demonstrably isolated work;
- wrapper timeout, dependency failure, assertion failure, and resource/order stall are reported distinctly;
- diagnostics expose current node/phase and slow boundaries without turning timeout into pass.

CC7 is not closed until local evidence supplies: a functioning optional parallel environment; repeated multi-worker measurements on the reviewed inventory; worker-state/resource-leak checks; an evidence-based recommended worker count, if parallelism is worthwhile; a completed ordinary full-suite run in the actual local environment where practical; investigation of any reproducible release-cutover/full-suite stall using the diagnostics.

Do not turn an unavailable package index or execution-wrapper cutoff into a passing claim.

## 5. Implementation order

Use vertical slices so each package ends in one coherent working path.

Default order: finish CC1 consumer/adapter closure → legal actions plus one workflow-transition family → request/replay plus one consequential-command family → operation/lease/reclaim family → semantic proposal/Human Review family → projection/effect-settlement family → retained persistence model and any migration rebaseline → remaining surfaces and compatibility removal.

The order may pause for dark-launch/product defects, but do not run several agents through shared core files simultaneously.

## 6. Recommended parallel wave shape

Once in-flight recovery/authority integration and dark-launch/frontend activation work have settled enough to establish file ownership, a safe parallel wave looks like:

- **CC1 tail** — command/transport consumer/adapter cleanup only, no workflow semantics;
- **CC7 diagnosis** — long-process/resource diagnosis, separate from optional xdist benchmarking;
- **CC5 dependency proof** — trace real writers/readers/lifecycle for a small named set of evidentiary/migration-state candidates, produce an implementation-ready disposition, no broad deletion;
- **CC6 narrow slice** — fix one proven dependency-direction violation outside moving workflow/recovery/effect core.

Each package needs explicit owned files and explicit exclusion lists before dispatch.

## 7. Local integration/evidence work that should not be outsourced blindly

Some work is best done against the real local environment/current main: activating and observing dark launch; exercising recovery/workflow behavior against live/local state; building an optional xdist environment from a functioning package source/wheelhouse; benchmarking and repeating the experimental parallel lane; completing ordinary/full or governed long-running test evidence; running native PostgreSQL evidence where DSNs are available; frontend read-core native `EXPLAIN`/bounds and activation gates; reconciling generated/governed baseline documents after integrating patches generated from older heads.

A patch generated against an older head does not require a new agent round when conflicts are mechanical and current-main semantics are clear. Request a new HEAD/rebase only when the agent genuinely needs current integration state to make a safe semantic decision.

## 8. Exit criteria

This program is complete when:

- one authoritative production command definition system remains;
- one authoritative workflow/authority implementation remains;
- one coherent request/replay/lease/operation model remains;
- one understandable external-effect lifecycle remains;
- package dependency direction is clear and enforced at meaningful boundaries;
- persistence concepts are justified by real invariants and consumers;
- temporary/evidentiary state has an explicit lifecycle rather than inertia;
- test bootstrap and planner-selected evidence are predictable;
- change fan-out is materially lower;
- dark-launch behavior remains stable, or any deliberate shadow rebuild/rebaseline is explicitly explained;
- critical modules can be understood without reading the entire repository.

## 9. Measures of success

Primary measures: number of authoritative implementations per rule; files changed per ordinary feature/defect; cognitive complexity in critical paths; dependency violations/cycles; time from receiving source to first valid test result; first-attempt reliability of governed tests; frequency of manual test sharding/reconstruction; dark-launch regression rate and diagnosis time; number of production concepts existing only to support tests or evidence bureaucracy.

Secondary measures (diagnostic only, count only when primary measures improve): line count; table count; file count; fixture count; document count.

## 10. Coordination discipline

For each agent package: state exact owned area/files and explicit exclusions; give the current source identity available to that agent truthfully; require a clean patch and concise handoff; report test/setup/tooling friction separately from implementation scope; allow at most one correction round for ordinary review findings before central integration/fix is preferred; do not ask for rebasing merely because generated docs or mechanically changed adapters drifted; stop and request current HEAD only when semantic reconciliation depends on changes the agent cannot see.
