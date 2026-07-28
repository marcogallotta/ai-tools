# Dish production rollout

Dish implementation and connectivity work is complete. Production activation is not. This is one
coordinated rollout of both the substantial Honest protocol/schema overhaul and the Dish tool that
enforces it. The Honest protocol bundle, repository routing, and Dish service are tested, activated,
and, if necessary, rolled back together; a mixed production state is forbidden.

This runbook owns the separately authorized migration rehearsal, joint test-project validation,
production cutover, and rollback. Passing the Dish automated suite, the Honest protocol tests, or
the Preview connectivity gate alone does not authorize production Cooking-task mutation.

## Pre-rollout review

Resolve and record these review items before production activation. Until a decision changes the
implementation or runtime documentation, the current code and contracts remain authoritative.

1. **Service-mode defaults.** Local direct mode remains available for controlled single-agent
   testing, while live clients currently fail closed unless both `DISH_LIVE_MODE=1` and
   `DISH_MODE=service` are set. Decide whether production should keep both explicit gates or make
   service mode the operational default.
2. **Real-schema SDK lifecycle coverage.** The generated Asana SDK lifecycle test traverses
   `DishApplication` → `AsanaBackend` → generated SDK → stateful fake HTTP transport, but its release
   fixture uses `schema={}`. Decide whether activation requires the same boundary test to load the
   complete current Honest schema fixture.
3. **Public-endpoint abuse controls.** The Action listener has a dedicated credential, route
   allowlist, body limits, request timeouts, and no private or admin routes. Decide whether Funnel
   exposure also requires an application-level rate limiter.
4. **Production authorization.** Implementation and connectivity gates are complete, but the full
   test-project mutation smoke, migration rehearsal, backup/restore rehearsal, rollback
   confirmation, production credential and section-registry verification, canary migration, and
   production cutover still require explicit authorization. The rollout `TODO` lists a canary,
   while `dish-docs-design.md` says corpus migration requires no canary or per-task semantic
   attestation. Resolve whether the canary is an operational rehearsal or is removed; never use it
   as semantic attestation or a claim of semantic equivalence.

## Test-project rehearsal

Use the isolated rollout checkout, test project, database, and backup directory documented in
[`../README.md`](../README.md). Never point this rehearsal at production Cooking.

1. Configure and verify the private Serve and public Funnel paths using
   [`../deploy/tailscale/README.md`](../deploy/tailscale/README.md).
2. Complete the GPT editor Preview gate in
   [`../deploy/gpt-action.md`](../deploy/gpt-action.md).
3. Run the complete disposable-task procedure in
   [`../deploy/live-test-project-smoke.md`](../deploy/live-test-project-smoke.md), preserving its JSON
   transcript.
4. Test the final Honest Planning, Research, and Verification protocols as one bundle through Dish.
   Exercise Planning, Research, Small, Large, Evidence, Human Review, Verification signoff, and
   final movement one agent at a time, checking both the protocol's semantic duties and Dish's
   deterministic enforcement.
5. Deliberately exercise a stale candidate, an out-of-band edit, an uncertain write, a migration
   failure, and a movement retry. Verify that Cooking reads only exact live `ready` tasks through the
   supported interface and that no command exposes another stage's protocol.
6. Rehearse managed backup and restore, then confirm the rollback inputs below are complete and
   usable.

Stop on any condition named in the smoke procedure. Resolve the failure and repeat the affected
gate; do not reinterpret a failed gate as acceptable for production.

## Corpus migration

Follow the approved corpus-wide procedure in `~/honest-pantry/dish-docs-design.md`:

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
2. Confirm service compatibility with that Honest release, production credentials, the Cooking
   project and section registry, and the approved GPT Action exposure/authentication route.
3. Run the complete bundled Honest protocol test and complete Dish suite, including service
   concurrency and restart tests. Confirm the CLI and GPT Action use the same endpoint result
   contract.
4. Confirm direct agent Asana write credentials and unsupported governed-task write paths are
   disabled. Keep Planning's deliberate read-only access to completed cooking history.
5. Confirm the joint test-project, migration, backup/restore, and rollback rehearsals passed.
6. Apply the resolved migration sequence. If an operational canary is retained, migrate one
   reviewed task and run one complete live lifecycle, checking exact content, placement, recovery,
   leases, and audit evidence without treating it as semantic attestation.
7. Migrate only the approved target corpus, then activate the matching Honest protocol authority,
   repository routing, and Dish production service in the same deliberate cutover.
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
isolation, migration, backup/restore, recovery, the resolved canary decision, joint cutover, and
rollback gates.
