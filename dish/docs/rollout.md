# Dish production rollout

Dish implementation and connectivity work is complete. Production activation is not. This is one
coordinated rollout of both the substantial Honest protocol/schema overhaul and the Dish tool that
enforces it. The Honest protocol bundle, repository routing, and Dish service are tested, activated,
and, if necessary, rolled back together; a mixed production state is forbidden.

This runbook owns the separately authorized migration rehearsal, joint test-project validation,
production cutover, and rollback. Passing the Dish automated suite, the Honest protocol tests, or
the Preview connectivity gate alone does not authorize production Cooking-task mutation.

## Completed rollout evidence

The detailed run IDs, fixture tasks, revisions, and transcript locations remain in
[`../deploy/live-test-project-smoke.md`](../deploy/live-test-project-smoke.md). The following work is
complete and need not be repeated wholesale:

- Dish implementation, service connectivity, private/public listener separation, and credential
  scopes have passed automated and live test-project coverage.
- Codex automated smoke testing is complete for its tested revisions. If later code changes land,
  select regression coverage in proportion to the affected authority and commits since the last
  recorded pass; documentation-only changes require no test rerun.
- A connected GPT completed create → Planning → Research → independent Verification → submit, with
  exact content identities, placement, request replay, and final signoff confirmed. The custom GPT
  Action lifecycle testing is complete.
- A fresh Small-correction lifecycle, Action lease-renewal replay/conflict, and failed-first
  validation replay/conflict passed.
- The exact GPT editor Preview gate in [`../deploy/gpt-action.md`](../deploy/gpt-action.md) is
  complete.
- Private lease recovery and governed-change authorization passed with durable binding, exact
  replay, and changed-payload conflict.
- Managed backup and restore passed with exact installed identity, restored durable state, healthy
  readiness, and owner-only database permissions.
- The broad final code audit is complete. It confirmed the earlier concurrency, recovery,
  authorization, submission, backup, and replay fixes. Its two audit blockers—immediate admin
  recovery under a live Action lease and atomic pre-construction Research hold/audit persistence—
  are fixed with deterministic regressions.
- The release-specific evidence record and final semantic rehearsal in the Honest rollout checkout's
  [`rollout.md`](../../../honest-pantry/rollout.md) are largely complete, including
  Planning, Research, and Verification protocol testing through Dish's bounded regression set with
  sound positive controls and known material failure shapes.
- **Abandoned run ownership, Part I** is complete. The pre-rollout abandonment patch —
  `abandon-operation` and `reconcile-abandonment`, stage policy, the durable abandonment/succession
  records, and the pre-construction Research reject lease-reacquisition fix — is implemented and
  documented in `docs/architecture.md` and `docs/runtime-contract.md`.
  [`abandoned-run-ownership-design.md`](abandoned-run-ownership-design.md) Part I is now a historical
  summary, not a live spec. Part II of that document is a post-rollout draft, reopened for review
  only, not launch scope; see below.

This evidence does not replace the final Honest semantic rehearsal, release-specific regression
selection, migration rehearsal, rollback proof, or production authorization.

## Remaining pre-rollout review

Resolve and record these review items before production activation. Until a decision changes the
implementation or runtime documentation, the current code and contracts remain authoritative.

1. **Final regression gate.** Preserve the deterministic recovery and governed-audit regressions,
   then pass focused concurrency/recovery coverage plus the complete suite on the final code.
2. **Production authorization.** Migration rehearsal, rollback confirmation, production credential
   and section-registry verification, and production cutover still require explicit authorization.

## Post-rollout: abandoned run ownership Part II review

Not launch-blocking. Part I has shipped, so the design document's Part II is reopened for review
comments, but it remains a draft that is not ready for implementation and must not add
requirements to Part I.

1. Commission a full ChatGPT review of Part II once production evidence from Part I is available.
2. Follow with a focused Claude review of Part II's proposal against the invariants most prone to
   silent drift across files: cross-cutting `allowed_actions`/current-action projection consistency
   (`workflow_policy.py`, `application_service.py`, `step5.py`–`step9.py`, `http.py`,
   `command_spec.py`), the transaction-atomicity claims implied by the new attempt/session model, and
   the writer-lock/external-effect boundary trade-off carried over from Part I's abandonment
   transactions.

## Remaining test-project rehearsal

Use the isolated rollout checkout, test project, database, and backup directory documented in
[`../README.md`](../README.md). Never point this rehearsal at production Cooking.

1. Review code commits since the last recorded automated pass and run proportionate regression
   coverage against the frozen release revisions. Use the complete Dish suite only when the
   intervening workflow, persistence, concurrency, recovery, migration, or service-boundary changes
   warrant it.
2. Close the activation record with one frozen Honest revision, one frozen Dish revision, verified
   runtime configuration and health, and durable redacted transcript locations outside `/tmp`.
   Reuse applicable recorded smoke evidence rather than rerunning every earlier adversarial case.
3. Confirm stale-state, uncertain-effect, migration-failure, and movement-retry enforcement through
   the existing authoritative automated or local fault-injection coverage. Do not require a
   connected agent to manufacture an unsafe state that the Action surface cannot legally create.
4. Confirm the rollback inputs below are complete and usable.

Stop on any condition named in the smoke procedure. Resolve the failure and repeat the affected
gate, or document it under the established launch-triage policy when it is proven low-risk and
fail-safe; do not silently reinterpret a failed gate as a pass.

## Corpus migration

Follow the approved corpus-wide procedure in the frozen Honest rollout revision's
[`dish-docs-design.md`](../../../honest-pantry/dish-docs-design.md):

1. Snapshot the complete target corpus to a tarball.
2. Give a fresh agent the snapshot and final protocol bundle. Produce the migrated corpus locally,
   removing legacy structure without inventing content that requires judgment.
3. Run deterministic validation over every result. Return every structural failure for correction
   until the corpus passes.
4. Upload through a script that stops instead of overwriting when a live task no longer matches its
   snapshot input.
5. Never infer `ready`, provenance, Human decisions, or destination data.
6. Activate the new protocol authority and repository routing deliberately; do not leave a mixed
   production state.
7. Retain the old project and database snapshot until the migration is accepted.

`dish-admin migrate` is only for an individual older-schema task encountered after cutover. It is not
the initial corpus-migration mechanism. Completed historical tasks remain untouched unless
deliberately reopened, when they must migrate before substantive work.

## Production cutover

Production cutover requires a separate explicit authorization. After authorization:

1. Freeze and record the exact compatible Honest protocol/schema revision and Dish code/tool
   revision being released together.
2. Stop the test-configured service. Replace its rollout values with the frozen production Honest
   checkout, production `DISH_COOKING_PROJECT_GID`, a fresh production `DISH_DB_PATH`, and a
   production `DISH_SERVICE_BACKUP_DIR`. Do not copy or reuse the test service database as
   production state.
3. Restart the service and confirm its configured database path, owner-only state and backup
   directory, production Cooking project and section registry, production Asana credential,
   compatibility with the frozen Honest release, and GPT Action exposure/authentication route.
   Do not admit production mutations while any test checkout, database, backup directory, project,
   or section GID remains configured.
4. Confirm the Honest-side completion gate passed. Review Dish commits since the last recorded
   automated pass and run the proportionate final regression set, including service concurrency and
   restart coverage when those boundaries changed. Confirm the CLI and GPT Action use the same
   endpoint result contract.
5. Confirm direct agent Asana write credentials and unsupported governed-task write paths are
   disabled. Keep Planning's deliberate read-only access to completed cooking history.
6. Confirm the joint test-project, migration, backup/restore, and rollback rehearsals passed.
7. Apply the resolved migration sequence only to the approved target corpus, then activate the
   matching Honest protocol authority, repository routing, and Dish production service in the same
   deliberate cutover.
8. Open broader use only after the final protocol, lock, drift, recovery, and audit checks pass.

## Rollback

Before cutover, prove that rollback can restore all of:

- the prior supported Honest protocol, schema, and repository-routing set;
- compatible Dish tool and service code;
- the shared database backup;
- exact task snapshots for every migrated task.

Do not reopen writes under a mixed protocol, schema, tool, or database combination. If a cutover gate
fails, stop mutations, preserve the live evidence and transcripts, and restore the complete
compatible set.

## Completion gate

Dish is live only after explicit authorization and successful exercise of compatibility, shared
coordination, the complete Honest protocol bundle, Dish enforcement, exact-content handling, stage
isolation, migration, backup/restore, recovery, joint cutover, and rollback gates.
