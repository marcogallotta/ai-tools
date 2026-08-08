# PostgreSQL dark-launch ops issues

Tracks the status of hardening/gap items raised by ChatGPT during dark-launch
cutover review, cross-checked against actual repo state. This is a snapshot,
not a live source of truth — it will go stale the same way
`database-backend-production-change-ledger.md` did, so check the "Last
verified" column before trusting an "open" or "done" mark on anything more
than a few weeks old.

Snapshot date: 2026-08-04, refreshed 2026-08-05, 2026-08-07, 2026-08-08. Verified repair tree
through `09fa713` (synthetic Git history created from the supplied repository snapshot). The
2026-08-08 refresh directly re-verified the "Confirmed open — verified directly against code"
table and the §3 runtime-wiring blocker against current code and a real native rehearsal run; the
ChatGPT-claim-only tables below were not re-checked in that pass.

## Priority key

- **Must-fix** — gates trusting a real cutover; wrong even without concurrent
  actors, or directly controls the irreversible transition.
- **Later** — real gap, but not cutover-gating; worth doing eventually.
- **Skip** — theoretical race/gap that needs genuinely concurrent conflicting
  actors to trigger; unlikely to matter for a single-operator system.
- **Decision** — priority depends on an architectural scope decision not yet
  made (e.g. whether PostgreSQL must own a workflow post-cutover); do not
  read as resolved either way until that decision is made.

## Owner key

- **ChatGPT** — pure code change, no local dependency.
- **Mixed** — ChatGPT can write the code, but the fix must be verified
  against native PostgreSQL / real local state, or requires local input
  (e.g. which Asana fields to trust) to implement correctly.
- **Local** — the work itself is an observation, operational action, or
  decision, not code.

## Local-effort key

Effort of the *local verification/execution*, not the code change: Easy
(rerun an existing test lane), Medium (a dedicated local/native run),
Hard (requires live fault injection, killing processes mid-transaction,
or real external-system state).

---

## Done — verified implementation

| Item | Last verified | Evidence |
| --- | --- | --- |
| First-request reservation/consumption mechanism | 2026-08-04 (fork-verified) | `dish_pg/reservation_models.py`; commits `0f1d380`, `0b1ef7d`, `54ede4b`, `e13c52f` |
| Shadow-baseline disqualification mechanism | 2026-08-04 (this session's fix) | `3f45c5f`, `b90f42c` |
| Writer-fence artifact observation (filesystem identity evidence) | 2026-08-04 (fork-verified) | Commits `ec54c21`, `9867978`, `d2b89c1` |
| Typed/sealed readiness evidence against a supplied inventory | 2026-08-04 (fork-verified) | Commit `de0e249`, migration `0026` |
| Post-burn admission prerequisite records | 2026-08-04 (fork-verified) | Commit `22ce555` |
| Source (SQLite) semantic-proposal workflow, full lifecycle | 2026-08-04 (fork-verified) | Commits `5cfcdfc`, `bff1c0d` |
| Test breakup / changed-path test-selection infra | 2026-08-04 (confirmed via `git log` independent of ChatGPT) | Commits `227e681`, `dd95d70`, `24d705d` |
| PGlite / governed native test-lane infra | 2026-08-04 (confirmed via `git log` independent of ChatGPT) | Commits `95ebdff`, `fdaa678`, `6f04142`, `afda679` |
| Legacy-writer inventory — enumeration | 2026-08-04 (repo search + Marco confirmation) | Complete set: `dish-service-prod.service`, `dish` CLI, `dish-admin` CLI. Repo search of `scripts/` found no other write path to legacy SQLite/Asana — the `dish-pg-*` scripts that touch SQLite operate on disposable rehearsal/test spool DBs, not authoritative state. Marco confirmed no writer exists outside the repo (no cron/manual/external process). |
| Legacy-writer inventory — enforcement | 2026-08-05 (directly code-verified, fixed and tested) | `validate_writer_fence_observation()` (`release_validation.py:310-382`); threaded through `verify_writer_fence()`, `mark_fenced()`, `activate_authority()`, and `burn_rollback()` (`cutover_control.py`); commits `1d4729b`, `b7d0d0f`, `9c18ea6`. Exact set-equality against `required_writer_inventory`, fails closed on `None`/empty/blank/duplicate, correct `missing_writer_targets`/`extra_writer_targets` diagnostics. `burn_rollback()` was missing the parameter entirely (`TypeError` on every call, caught by rerunning the `release-cutover` test lane) — fixed in `9c18ea6`. `scripts/dish-pg-release` now passes the real confirmed inventory (`dish-service-prod.service`, `dish`, `dish-admin`) to all four cutover subcommands. `release-cutover` lane: 75 passed. |
| Writer-fence planned/deployed binding | 2026-08-05 (directly code-verified) | `legacy_writer_fence.py`; commit `b7d0d0f`. SHA-256 digest comparison between prepared manifest and deployed on-disk bytes, checked before every state transition including idempotent re-entry. |
| Rollback-burn skips fresh quiescence evaluation | 2026-08-05 (directly code-verified) | `burn_rollback()` now reruns `evaluate_candidate()` immediately before the irreversible transition (`cutover_control.py:661-668`), fails closed on any failed check. Commit `fce152c`, migration `0029`. |
| First-request admission reopens before `verify_first_admission()` | 2026-08-05 (directly code-verified) | `verify_first_admission()` (`cutover_control.py:1221-1238`) is the only path that opens general mutation admission; consuming the reservation alone leaves admission closed. Commit `fce152c`, migration `0029`. |
| Missing generation-bound candidate FKs | 2026-08-05 (directly code-verified) | Migration `0029` adds composite FK constraints binding each candidate's `source_import_batch_id`/`shadow_baseline_id`/`projection_epoch_id` to its own `generation_id`, plus a populated-data preflight that aborts on existing lineage mismatches. Commit `fce152c`. |
| Illegal candidate initial states admitted on INSERT | 2026-08-05 (directly code-verified) | Migration `0029` adds a `BEFORE INSERT` guard trigger on `release_candidates` (`release_candidates_initial_state_guard`). Commit `fce152c`. |
| Illegal admission-control / reservation initial states | 2026-08-05 (directly code-verified) | Migration `0029` adds `BEFORE INSERT` guard triggers on `mutation_admission_controls` and `first_request_reservations`. Commit `fce152c`. |
| Migration `0028` fails open when no admission-control row exists | 2026-08-05 (directly code-verified) | Migration `0029` adds `mutation_admission_controls_verified_open_guard` (`BEFORE UPDATE`) and `service_requests_stage6_admission_guard` (`BEFORE INSERT`), closing the fail-open path. Commit `fce152c`. |
| Stage A treatment/baseline evidence | 2026-08-05 (directly generated and tested) | Governed generator records exact `source_only_commands` (`proposals`, `apply-proposal`, `review-approve`, `review-inspect`, `review-reject`, `review-queue`), canonical baseline SHA-256 `26d456e648e3e1e9b0a507de6483b675b1abe1cac80c94b32aeea77d76044ab5`, post-write `--check` passed, and `test_stage1_baseline_contract.py` reported 5 passed. Commit `09fa713`. |
| Agent `inspect` request identity and ambiguous-response replay | 2026-08-06 (directly code-verified and tested) | Private and Action clients determine the request UUID before dispatch and surface the exact transmitted request/run identity as non-retryable `BACKEND_UNCERTAIN` after transport loss, invalid JSON, unreadable or empty bodies, and canonical-envelope validation failure. The CLI emits the exact replay argv, environment, and shell command. Real post-execution response-replacement regressions prove manual replay returns the authoritative stored result, changed-payload reuse conflicts, no automatic retry occurs, and only one `dish_inspect_fact` exists. |
| `apply-proposal` client request identity and ambiguous-response replay | 2026-08-06 (directly code-verified and tested) | Generated and explicit request UUIDs survive transport loss, invalid JSON, unreadable or empty bodies, and canonical-envelope validation failure through private, Action, and CLI surfaces. Exact manual replay returns the first stored application result, changed-payload reuse conflicts, no second backend write occurs, and no additional Verification cycle is created. The client never retries the consequential call automatically. |
| Projection dispatch kill switch | 2026-08-05 (directly code-verified) | `ProjectionService.begin_attempt()` locks the event/epoch path and rechecks active epoch, `external_effects_enabled`, and active generation before dispatch (`dish_pg/transition.py`). |
| Projection epoch retirement serialization | 2026-08-05 (directly code-verified) | Event insertion takes the active epoch shared lock; retirement takes the exclusive epoch lock, then locks non-terminal events and active attempts before superseding/blocking them (`dish_pg/transition.py`). |
| Shadow-delivery settlement authority | 2026-08-05 (directly code-verified) | `_assert_delivery_claim()` checks state, token, owner, revision, and unexpired lease; `_settle_delivery_cas()` repeats them in the conditional update (`dish_pg/transition.py`). |
| Final-evidence completion gate | 2026-08-05 (directly code-verified) | `complete_cutover()` rebuilds and validates a fresh `cutover_final` evidence bundle before advancing to `completed` (`dish_pg/cutover_control.py`). |
| Shared service/local process lock | 2026-08-08 (directly code-verified) | `DatabaseProcessLock` guards service, local `dish`, and local `dish-admin` access to the governed SQLite database; service startup passes `role="service"` and `rule="service_process_lock_held"` directly (`dish_service/__main__.py`, `dish_service/cli.py`, `dish_service/admin_cli.py`). |
| Migration-target helper current head | 2026-08-07 (directly code-verified) | Release/bootstrap/acceptance helpers derive or assert `0031_worker_readiness_consolidation` through `ALEMBIC_HEAD`/`DEFAULT_SCHEMA_HEAD`; the Stage 6 runbook names the same checked-in head. |
| `test_native_initial_state_insert_guards_reject_direct_sql` fails against live PostgreSQL | 2026-08-08 (directly code-verified and fixed) | `engine.raw_connection()` returns SQLAlchemy's pool proxy, which only overrides `__getattr__`; setting `.autocommit` on it never reaches the real psycopg connection. Fixed by setting `raw.driver_connection.autocommit = True` instead (`tests/support/postgresql/native_first_request_reservation_single_gate.py:135`). Verified passing. |
| Shadow-baseline capture vs close/disqualify race | 2026-08-08 (directly code-verified) | `capture_envelope()` and `close_baseline()` (`dish_pg/transition.py:381,895`) both go through `_lock_baseline()`, which applies `.with_for_update()` on PostgreSQL. Not an unlocked read; the described race does not exist in current code. |
| Stale `dish_pg/shadow_worker.py.orig` hygiene claim | 2026-08-05 (directly tree-verified) | No `.orig` file exists under `dish_pg` in the supplied snapshot. |
| Stage 6 runbook migration-head claim | 2026-08-07 (directly doc-verified) | `docs/database-backend-stage6-runbook.md` consistently names `0031_worker_readiness_consolidation` as the checked-in target; historical migration references remain explicit where needed. |

## Provisionally done — not independently reverified

| Item | Last verified | Evidence |
| --- | --- | --- |
| Whole-source import hash/count checking | 2026-08-04 (ChatGPT claim only) | Claimed complete in 2nd-round audit; not independently checked against code |

## Removed — not a defect or not a requirement

| Item | Why removed |
| --- | --- |
| Stage A "top-level success ⇒ production success" | `dish-pg-acceptance` already reports `acceptance_scope="source_contract"` separately from `production_acceptance_complete` |
| Source vs PG `inspect` identical request semantics | Not a real requirement: migration baseline treats target `inspect` as `retain:E`; only the deployed OpenAPI/instructions need to switch together |
| Closure-through-activation | Not a defect under current architecture; current `architecture/operations-leases-and-fencing.md` requires closure through the writer-fence boundary |

## Confirmed open — verified directly against code (high confidence)

| Item | Priority | Owner | Local effort | Last verified | Note |
| --- | --- | --- | --- | --- | --- |
| Dark-launch legacy import drops prior operation/lease history | Later | Mixed | Hard | 2026-08-08 (directly verified live against production legacy SQLite and PostgreSQL) | `dish_pg/importer.py` / `CoreAuthorityService.import_task_document` only imports task content/registry state, never `workflow_operations`/`service_leases`/etc. Any task with pre-existing legacy operation history at resync time gets treated as pristine in PostgreSQL, producing shadow-comparison `mismatch`/`delivery_failure` on its next captured command. Design+implementation handed to ChatGPT (extending import requires deciding how backfilled operations get authorized without a live service request, without weakening that invariant for live-authorized operations). |
| Dark-launch rollout-order stall: one permanently-failed delivery blocks all later evidence | Later | Mixed | Medium | 2026-08-08 (directly verified live) | Delivery claiming is strictly rollout-ordered (`claim_delivery` in `dish_pg/transition.py`): an envelope cannot be claimed while any earlier-rollout-sequence delivery for the same baseline is not yet `delivered`. A `failed` delivery is terminal and nothing currently converts it back to `delivered` except a full retry (which fails again for the same root cause above). Live consequence observed 2026-08-08: one `prepare` delivery failed with "no unique target operation binding," permanently blocking 15 newer captured commands from ever being evaluated. No operator tool exists to skip/void a permanently-stuck delivery and unblock what's behind it (`skip_delivery` in `dish_pg/transition.py` requires an active claim, so it can't be invoked on an already-terminal `failed` row). Revisit once the import-history fix above lands and load is lower — may resolve itself after a fresh resync, or may need its own recovery tool. |

## Partial — mechanism exists, real gap remains

| Item | Priority | Owner | Local effort | Last verified | Note |
| --- | --- | --- | --- | --- | --- |
| Canonical readiness inventory | **Resolved** | CC5 | — | 2026-08-08 (code/test verified in CC5 patch) | Forward revision 0031 removes the producer-orphaned typed inventory/requirement/evidence/completion chain. `projection_worker_readiness` is now the one immutable post-burn report; the validator owns the exact `claim`/`exact_write`/`restart` set and derives worker/release/artifact identity from runtime state. First admission revalidates exact fresh reconciliation plus report hash/identity/timing. |
| Post-burn admission manifest | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) | Prerequisites enforced, but approval-time candidate manifest not revalidated against post-burn-only inputs |
| Semantic-proposal PostgreSQL parity | Decision | ChatGPT+Mixed | Hard | 2026-08-04 (ChatGPT claim only) | Source side complete; PG-target authority not implemented. Priority depends on an unmade scope decision: Skip if semantic-proposal commands remain source-owned after cutover; Must-fix if PostgreSQL must own this workflow |
| Sealed per-entity source-import manifest | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) | Source verification exists; no persisted per-entity digest manifest |

## Confirmed open — claimed with specificity, not independently code-verified

Trust calibrated by the 5/5 hit rate on the code-verified table above, but
still unchecked directly.

| Item | Priority | Owner | Local effort | Last verified |
| --- | --- | --- | --- | --- |
| Planning-challenge settlement not single-winner | Skip | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Source-import concurrency (lost counter increments) | Skip | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Verification/`inspect` read-model parity (internal `verify` leaks into public continuation) | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Independent reconciliation membership (expected corpus derived circularly from existing mappings) | Later | Mixed | Hard | 2026-08-04 (ChatGPT claim only) |
| Offline Alembic support (version-width contract mismatch) | Later | ChatGPT | Medium | 2026-08-04 (ChatGPT claim only) |
| External-effect ABA protection (no proof external version unchanged) | Later | Mixed | Hard | 2026-08-04 (ChatGPT claim only) |
| PostgreSQL contention classification (`23505`/`40P01`/`40001` not mapped to retry) | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Backup ambiguity / reservation recovery on rename-fault | Skip | Mixed | Hard | 2026-08-04 (ChatGPT claim only) — see `database-backend-postgresql-test-plan.md` §2 |
| General PostgreSQL commit-before-response / lost-response replay harness beyond consequential `inspect` and `apply-proposal` | Later | Mixed | Hard | 2026-08-06 — the source-service/client boundary for those two calls is now directly covered; broader PostgreSQL worker/process-loss coverage remains open — see `database-backend-postgresql-test-plan.md` §1 |

## Cat-3 repo hygiene — claimed pure repo fixes, not independently verified

| Item | Priority | Owner | Local effort | Last verified |
| --- | --- | --- | --- | --- |
| Missing semantic-invariant diagnostic mappings in `_semantic_relationship` | Later | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Admin command metadata scattered across `admin.py`/`admin_cli.py`/`application.py` | Skip | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Read-only submission/destination authority still imported from `step9` | Skip | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Three missing ORM index declarations (present only in migrations) | Later | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Nested continuation still exposes internal `verify` action | Later | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| `DishService` typed seams incomplete (coordinators still accept `service: Any`) | Skip | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |

## Docs needing refresh

| Doc | Issue | Last verified |
| --- | --- | --- |
| `docs/database-backend-production-change-ledger.md` | Stale — last reviewed through `42619b9` (2026-08-01), well behind current HEAD as of this snapshot | 2026-08-04 (fork-verified) |
