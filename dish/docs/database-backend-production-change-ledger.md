# Database backend production-change ledger

Status: backfilled, reviewed, and Stage 1 contract-closed through commit `42619b9` (2026-08-01 20:47).

Scope: every commit merged/deployed under `dish/` on or after 2026-08-01, screened
against the in-scope criteria in `database-backend-imp.md` §1.1. Reviewer: Claude
(coordinator), from `git log`/`git show` against the authoritative repository history.

## In-scope commits

| Commit | Time | Summary | Affected area | Disposition |
| --- | --- | --- | --- | --- |
| `e4edff2` | 22:21 | Clarified that ordinary Verification start omits both continuation targets; the exact operation/cycle pair is accepted only when Dish returns it for abandonment continuation. | Public protocol/command surface | Target contract must preserve the distinction: ordinary Verification resolves its one current cycle, while abandonment succession supplies an exact paired target and never derives it from `submission_id`. |
| `a2a9b52` | 17:07 | Made `CurrentWorkflowService`/`workflow_policy.legal_actions` the sole owner of legal-action derivation; removed `ALLOWED_ACTIONS_BY_STATE`; renamed `legal_operation_actions` to `phase_candidate_actions`. | Command/action authority | Already covered and Stage 1 closed: §12 now requires the PostgreSQL single-task policy and bounded set-oriented query to consume the same declared predicates, with equivalence contract tests and no second action matrix. |
| `7f2f114` | 14:26 | Cooking-project placement selected by exact identity; Asana create/section-move mutations reread and confirmed; partial-application evidence reported. | External-effect/projection semantics | Implementation/migration document update required: Stage 5 shadow/projection design must replicate this confirm-and-reread contract. |
| `6075321` | 20:11 | New read-only `section-tasks` Action/CLI command listing tasks placed in a Cooking section. | Public protocol/command surface | Already covered and Stage 1 closed: §4 now retains it as a PostgreSQL Q-profile query over authoritative registry/location state. |
| `873ed5d` | 20:29 | Paginated `section-tasks` (opaque `next_cursor`); updated command/CLI/schema/OpenAPI and both GPT instruction docs. | Public protocol/command surface | Already covered and Stage 1 closed: the §4 row binds the opaque cursor to registry, section, ordering, and page boundary and fails closed on stale/mismatched use. |
| `dd277f1` | 20:44 | `future.md` note: phase-authoritative pending-Research/Verification listing deferred to Stage A schema/read model. | Documentation only, cross-reference | Already covered: matches Stage A's existing read-model intent (§12); no new decision needed. |
| `f0bbd51` | 17:10 | Split `initialize_database` (full init/migration/audit) from `open_runtime_database` (fast runtime open); first-caller bootstrap fallback. | Schema/migration bootstrap | Implementation/migration document update required: target bootstrap design should preserve this same init/runtime-open split as current authoritative behavior. |
| `cc6b32c` | 17:19 | Fixed a durable migration initializer import introduced by `f0bbd51`. | Schema/migration bootstrap | Already covered under the `f0bbd51` review. |
| `a7c0aac` | 17:11 | Retired compatibility wrappers/aliases; standardized on `pending_operation_steps`/`phase_candidate_actions` vocabulary. | Command/terminology surface | Already covered: confirms canonical current-system names the §4 contract and target schema should track; no conflict found. |
| `71cb6a2` | 17:07 | Retired legacy executable historical-submission mutation surfaces (second workflow engine); kept read/migration/reconciliation paths. | Command/authority surface | Already covered: narrows current surface toward what Stage A already scoped; no widening required. |
| `6efa0f0` | 17:08 | Added a shared audit-repair failure boundary (`audit_repair_processing_warning`) and structured lease-release cleanup evidence on rejected-command cleanup failure. | Audit/recovery | Implementation/migration document update required: §6.18 target audit/repair model should account for this outcome and evidence shape. |
| `be6725c` | 17:08 | Typed restore checkpoints (`RestorePlan`) and abandonment succession (`AbandonmentSuccessionSpec`). | Restore/abandonment | Implementation/migration document update required: §6.17 and restore-checkpoint handling should be checked against these typed field shapes. |
| `73c11de` | 10:00 | Fixed legacy backup taking a stale schema-version read vs. a concurrently migrated live database; added `read_transaction` snapshot primitive. | Backup/restore | Already covered: confirms backup snapshot correctness Stage A's baseline-capture step can assume as of this commit. |

## Reviewed, out of scope

No target-design impact found; behavior unchanged or non-persisted:

- Pure test-only refactors and fixtures: `66b26503`, `4407cfd`, `47db7ee`, `c2b0aae`,
  `4f7063d`, `4dfd865`, `4886960`, `3145bbd`, `8e22390`, `8695f3a`, `086fbeb`,
  `4c27dd6`, `be78a98`, `035ea74`, `850aec8`, `06aa117`, `b3fef48`.
- Doc/tooling cleanup with no behavior change: `43fa0c6`, `6460bbc`.
- Structural-only refactors with no external semantic change: `353b13c`
  (coordinator/route extraction), `aebbf89` (import-cycle removal).
- Infra with no database-authority effect: `5b0b96d` (Caddy Etag path),
  `13e1c6d` (dual-environment routing/client profiles).

## Excluded: prior durable-state import migration

`eb00d8f`, `2a6b378`, `dce866b`, `089d491`, `c00f433` complete a separate,
already-finished corpus/durable-state import migration under `dish/migration/`
(tracked by `dish/migration/corpus-migration-status.md`). This is not part of
Stage A's PostgreSQL migration; its end state is simply part of the "current
system" that Stage A's characterization must start from, not a change requiring
reconciliation into the target design.

## Pending production change

The 2026-08-02 Verification-hold worktree change is reviewed as in scope pending its final commit
identifier. It raises the hold threshold to the third non-approved round, replaces the durable
`two-pass-hold` outcome and `two_pass_resets` table with threshold-agnostic Verification-hold names,
adds the Marco-only `resolved` release command, and adds migration 36. The Stage A baseline,
SQLite characterization, migration/recovery fixtures, command inventory, and target treatment are
updated in this worktree. Replace the `WORKTREE` baseline source marker with the final commit before
release or cutover evidence is closed.


The current dark-launch worktree adds a fail-open local completion capture, durable spool, explicit
command-treatment registry, effect-disabled projection epochs, PostgreSQL shadow worker, status and
kill-switch controls, and deterministic legacy-source export. A subsequent safety fix adds immutable
`live`/`shadow` projection-outbox origin and makes projection claims exclude shadow rows regardless
of epoch effect state. The shared kill switch also halts the shadow worker before further delivery or
evaluation. Record each staged archive's final commit identifier here before selecting a
dark-launch source release. These changes do not transfer production authority or enable Asana
projection.

Commits `ae9936a` and `32cd14e` close the seven pre-dark-launch audit findings: filesystem
path aliasing and accidental spool creation, completion-time spool capacity enforcement, versioned
cross-backend response/state/effect comparison, source/target generation fencing, strict rollout
sequence claiming, and exactly idempotent gap delivery with worker backoff on deterministic spool
delivery failures. The Stage A source and characterization hashes are refreshed through the exact
reviewed source commit `32cd14e4d85761dceaf83c65728e5848a149c006`.

## Ongoing obligation

This ledger must be extended for every further in-scope commit through the exact
source commit/release selected for production cutover, per `database-backend-imp.md`
§1.1 and §14.10.

## 2026-08-03 PostgreSQL authority audit remediation worktree

The supplied post-dark-launch archive contained no Git metadata, so this entry uses the source
marker `WORKTREE-AUDIT-REMEDIATION-20260803` until the changes are committed in the authoritative
repository. The worktree closes the reproduced PostgreSQL authority defects found in the 2026-08-03
audit without enabling PostgreSQL authority or Asana projection:

- canonical Dish document parsing and exact rendering now gate authority activation and read-side
  workflow derivation;
- ordinary Verification start attaches the exact independent verifier occurrence and lease to the
  current operation/cycle, while paired targets remain limited to exact continuation targeting;
- approval requires complete reviewed-identity and semantic/provenance evidence, creates a signed
  canonical `ready` occurrence, and submission derives its destination from that signed document;
- deterministic command failures after request admission are stored as immutable replay outcomes,
  and expected handler failures roll back partial domain effects before the outcome is recorded;
- operation-targeted mutations require an exact operation identifier rather than inferring one;
- Planning confirmation is bound to the registered agent and complete issued target;
- retained `holds`, `resolved`, and `planning-intent-settlement` command inventories are aligned;
- Evidence, Human Review, and Verification holds produce canonical held/resumed occurrences with
  exact hold identity and legal continuation;
- migration `0015_verification_cycle_sequence` adds deterministic per-operation Verification-cycle
  ordering so same-timestamp cycles cannot select stale evidence.

The Stage A baseline hashes and command inventories are refreshed to this exact worktree. Replace the
worktree marker with the final commit identifier before using this baseline as release, dark-launch
comparison, rollback-burn, or cutover evidence.

## 2026-08-03 semantic-proposal review queue worktree

Source marker: `WORKTREE-SEMANTIC-PROPOSALS-20260803` until committed in the authoritative
repository. This worktree adds source-authority-only durable semantic proposal bundles for Large
Verification corrections that require Marco approval. It adds a review queue, atomic linked
authorization, claimable fresh-run application, exact candidate persistence, rejection-to-fresh-cycle
continuation, and append-only proposal/audit evidence. The current PostgreSQL authority target does
not yet implement these commands; `proposals`, `apply-proposal`, and `review-*` are therefore listed
explicitly in the Stage A baseline as `source_only_commands` rather than falsely treated as ported.
Before PostgreSQL authority activation, add target tables, transition semantics, command treatments,
shadow characterization, and migration evidence for this workflow, then remove the source-only
classification.
