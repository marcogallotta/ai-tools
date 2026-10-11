# Authority and data ownership

## Read this when

Read this when changing a durable fact, adding storage, changing a writer, or deciding whether a value is authoritative, derived, historical, or temporary.

## Scope

This document records durable authority boundaries. It intentionally avoids turning current implementation placement into permanent law.

## Authoritative implementation

Current anchors include `dish_tool/releases.py`, `dish_tool/task_document.py`, `dish_tool/database.py`, `dish_tool/task_store.py`, `dish_tool/workflow_policy.py`, `dish_service/request_replay.py`, and PostgreSQL models/services under `dish_pg/`. `dish_tool/recommendation_state.py` is a derived cooking-recommendation projection only; it is not a durable writer.

## Actors, processes, and stores

Current production combines Honest contract assets with PostgreSQL canonical Dish state/workflow. SQLite, archived Asana Cooking, and dark-launch artifacts are legacy/forensic compatibility evidence, not live authority.

## Authority and data ownership

| Fact | Current authority | Notes |
|---|---|---|
| Supported document contract | Honest assets | Parsed/validated forms are derived |
| Live title/notes/placement/completion | PostgreSQL Dish state and projections | Canonical |
| Workflow operations, replay, leases, audit | PostgreSQL workflow authority | Canonical |
| Legal workflow transitions | PostgreSQL command/workflow policy over authoritative state | Rendered actions are projections |
| Request identity/outcome | PostgreSQL replay authority | Permanent identity semantics |
| Kill request-to-revocation binding | PostgreSQL replay/run authority | Exact immutable request, operation, owner, run, source authority, and revocation identity |
| Native Section identity and catalog definition | `Section`, immutable catalog versions/entries/activations, and the CAS `ActiveSectionCatalog` pointer | Canonical definition and placement authority |
| SQLite / archived Asana | Legacy migration, forensic, and compatibility evidence | Never live Cooking authority |
| Dark-launch artifacts | Historical evidence only | Never current authority |
| Frontend password/session/limiter/audit state | Frontend security store | Access support only; never task or workflow authority |
| Frontend board/detail DTOs | Derived from the active canonical read source | Presentation snapshots; never writer or legality authority |
| Cooking recommendation evidence | The source authority named by immutable provenance (for example explicit user evidence, Cooking History, Scratchpad, Profile, or a runtime fact) | Recommendation state consumes evidence; it does not rewrite or duplicate the source lifecycle |
| Cooking recommendation current state/context | Derived by `dish_tool/recommendation_state.py` | Recomputable projection only. Exact candidate eligibility is evaluated outside the bounded soft slice; at most 32 soft signals enter the shared context, and missing exact eligibility is non-actionable |

## Invariants

- A consequential durable fact has one authoritative writer/decision boundary at a time.
- Derived views, caches, renderers, and transports may contain logic, but must not independently contradict the authoritative fact they represent.
- Missing or contradictory evidence is handled explicitly rather than treated as permission.
- Historical dark-launch evidence cannot silently supersede current authority.
- PostgreSQL native catalog/state owns current task placement and runtime reads.
- Archived Asana observations do not promote themselves back into canonical backend state.
- Frontend read projections, caches, presentation registries, and browser state cannot become
  task, placement, completion, workflow, or projection authority.
- Cooking recommendation state cannot become a competing Scratchpad/Profile lifecycle or safety/halal authority; inferred soft evidence cannot create a hard blocker or durable exclusion.
- Hard recommendation evidence coalesces implicitly only when both observations name the same
  stable fact key. Independent or unkeyed blockers and prerequisites remain active even when
  source, candidate, scope, and lifecycle shape match; clearing one event does not retire another.

Frontend security/session storage remains distinct from canonical PostgreSQL Dish authority.
Restoring security storage cannot by itself revive an expired, revoked, or superseded browser
session or rewrite task/workflow state.

## Process and transaction boundaries

Current PostgreSQL transaction-owner modules are implementation details; the architectural requirement is atomicity where identity/admission/outcome must agree and durable intent before uncertain effects when recovery depends on it.

## Normal flow

Read authoritative state, derive the decision, persist required intent/outcome evidence, perform any external effect, and expose derived views to clients.

## Failure, replay, recovery, and concurrency

Replay, fencing, and effect settlement preserve identity across failures. Store-specific locking is an implementation mechanism for those guarantees, not itself a product rule.

## Change routing

When changing authority, state clearly which fact changes writer/reader and what migration or compatibility boundary exists. Do not create a second writer merely because a new client or transport needs a different presentation.

## Proving tests

Evidence is distributed across workflow, replay, Asana lifecycle, recovery, and PostgreSQL authority tests, including `tests/test_workflow_policy_fail_closed.py`, `tests/test_request_identity.py`, `tests/test_recommendation_state.py`, and PostgreSQL authority tests under `tests/postgresql/`.

## Current debt and temporary compatibility

SQLite/Asana and other migration-era artifacts are legacy compatibility state and should remain only for concrete recovery/forensic needs. The `submissions` table and associated diagnostics remain compatibility state; production exposes no API that creates, transitions, or recovers a legacy submission.

## Related documents

- [System context](system-context.md)
- [Workflow and human review](workflow-and-human-review.md)
- [External effects and Asana](external-effects-and-asana.md)
- [PostgreSQL runtime](postgresql-runtime.md)
