# PostgreSQL dark-launch ops issues

Tracks the status of hardening/gap items raised by ChatGPT during dark-launch
cutover review, cross-checked against actual repo state. This is a snapshot,
not a live source of truth — it will go stale the same way
`database-backend-production-change-ledger.md` did, so check the "Last
verified" column before trusting an "open" or "done" mark on anything more
than a few weeks old.

Snapshot date: 2026-08-04. Repo HEAD at snapshot: `b90f42c` (main),
worktree `worktree-disqualify-baseline` not yet merged.

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

## Local runtime validation plan (must not be forgotten)

`database-backend-postgresql-test-plan.md` is the authoritative execution runbook for the
Hard-local-effort verification work — it is not optional background reading, it is the actual
procedure. Per that doc's own completion rule: "Local runtime validation is complete only when
Sections 1 through 4 have reproducible evidence." As of this snapshot, none of the four sections
have been run.

| Section | Covers | Status |
| --- | --- | --- |
| §1 Process-failure exercise | commit-before-response/lost-response replay, worker restart/takeover, PostgreSQL disconnect/recovery | Not run |
| §2 Backup, restore, and PITR rehearsal | backup/restore, PITR, RPO/RTO, fail-closed on corrupt backup | Not run |
| §3 Runtime wiring rehearsal | service + both workers against real PostgreSQL, cross-process proofs | Not run |
| §4 Production-shaped local rehearsal | full sequence against sanitized production-shaped data | Not run |

This is Must-fix, Local/Mixed, Hard local effort, gating a trusted cutover — do not let it get
dropped just because it isn't broken into individual claim rows below like the ChatGPT-sourced
items are.

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

## Provisionally done — not independently reverified

| Item | Last verified | Evidence |
| --- | --- | --- |
| Whole-source import hash/count checking | 2026-08-04 (ChatGPT claim only) | Claimed complete in 2nd-round audit; not independently checked against code |

## Removed — not a defect or not a requirement

| Item | Why removed |
| --- | --- |
| Stage A "top-level success ⇒ production success" | `dish-pg-acceptance` already reports `acceptance_scope="source_contract"` separately from `production_acceptance_complete` |
| Source vs PG `inspect` identical request semantics | Not a real requirement: migration baseline treats target `inspect` as `retain:E`; only the deployed OpenAPI/instructions need to switch together |
| Closure-through-activation | Not a defect under current architecture; current `architecture.md` only requires closure through the writer-fence boundary |

## Confirmed open — verified directly against code (high confidence)

| Item | Priority | Owner | Local effort | Last verified | Evidence |
| --- | --- | --- | --- | --- | --- |
| Rollback-burn skips fresh quiescence evaluation | Must-fix | Mixed | Medium | 2026-08-04 (fork-verified) | `burn_rollback()` (`cutover_control.py:514`) never calls `evaluate_candidate()`; only `activate_authority()` (line 430) does, where quiescence gating lives (`release.py:795-802`) |
| Shadow-baseline capture vs close/disqualify race | Later | Mixed | Medium | 2026-08-04 (fork-verified) | `capture_envelope()`/`close_baseline()` (`transition.py:232,632`) use unlocked `session.get()`, no `.with_for_update()` |
| First-request admission reopens before `verify_first_admission()` | Must-fix | Mixed | Medium | 2026-08-04 (fork-verified) | The reservation itself is exact, but once consumed, migration `0028`'s trigger returns `NEW` unconditionally for later unrelated requests — the system admits them before the reserved request has been verified. Proven by existing test `test_native_unrelated_valid_second_request_succeeds`. "Reservation mechanism done" and "admission state machine open" are not contradictory. |
| Missing generation-bound candidate FKs | Must-fix | Mixed | Medium | 2026-08-04 (fork-verified) | `ReleaseCandidate` (`stage6_models.py:34-44`) has independent FKs to generation/batch/baseline/epoch with no composite constraint tying them to the same generation |
| Illegal candidate initial states admitted on INSERT | Must-fix | Mixed | Medium | 2026-08-04 (fork-verified) | Migration `0022` installs guards for UPDATE/DELETE transitions only, not INSERT |
| Illegal admission-control / reservation initial states | Must-fix | Mixed | Medium | 2026-08-04 (fork-verified) | Same migration family; no INSERT-time guard on `mutation_admission_controls` or `first_request_reservations` |
| Migration `0028` fails open when no admission-control row exists | Must-fix | Mixed | Medium | 2026-08-04 (fork-verified) | `IF NOT FOUND THEN RETURN NEW; END IF;` (lines 52-54) |

## Partial — mechanism exists, real gap remains

| Item | Priority | Owner | Local effort | Last verified | Note |
| --- | --- | --- | --- | --- | --- |
| Legacy-writer inventory — enforcement | Must-fix | Mixed | Medium | 2026-08-04 (ChatGPT claim only) | Code gap: no exact-set-equality check between required and verified writers |
| Legacy-writer inventory — enumeration | Must-fix | Local | Hard | 2026-08-04 (ChatGPT claim only) | Only you can enumerate every real local writer (scripts, services, credentials, scheduled tasks, manual paths); this is not a code task |
| Writer-fence planned/deployed binding | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) | Artifact observed, but no digest comparison between planned manifest and generated on-disk manifest |
| Canonical readiness inventory | Later | Mixed | Easy | 2026-08-04 (ChatGPT claim only) | Typed evidence exists; probe kinds/contract versions still caller-supplied, no server-owned canonical registry. Typed readiness evidence is sufficient for launch if the locally supplied probe set is manually reviewed — the canonical registry is later hardening, not a launch blocker, unless admission or irreversible burn ends up relying on those probes as the decisive proof of safe operation. |
| Post-burn admission manifest | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) | Prerequisites enforced, but approval-time candidate manifest not revalidated against post-burn-only inputs |
| Semantic-proposal PostgreSQL parity | Decision | ChatGPT+Mixed | Hard | 2026-08-04 (ChatGPT claim only) | Source side complete; PG-target authority not implemented. Priority depends on an unmade scope decision: Skip if semantic-proposal commands remain source-owned after cutover; Must-fix if PostgreSQL must own this workflow |
| Sealed per-entity source-import manifest | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) | Source verification exists; no persisted per-entity digest manifest |
| Stage A treatment/baseline evidence | Later | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) | Currently failing (4 failed/5 passed); stale hashes/corpus, missing coverage for new source-only proposal commands |

## Confirmed open — claimed with specificity, not independently code-verified

Trust calibrated by the 5/5 hit rate on the code-verified table above, but
still unchecked directly.

| Item | Priority | Owner | Local effort | Last verified |
| --- | --- | --- | --- | --- |
| Projection dispatch kill switch (`begin_attempt()` doesn't recheck `external_effects_enabled`) | Must-fix | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Projection epoch retirement not serialized against event insertion/settlement | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Shadow-delivery lease settlement lacks expiry/owner/revision enforcement | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Planning-challenge settlement not single-winner | Skip | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Source-import concurrency (lost counter increments) | Skip | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Verification/`inspect` read-model parity (internal `verify` leaks into public continuation) | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Independent reconciliation membership (expected corpus derived circularly from existing mappings) | Later | Mixed | Hard | 2026-08-04 (ChatGPT claim only) |
| Final-evidence completion gate missing (cutover can complete before final bundle validated) | Must-fix — pending a check whether anything reads `completed` downstream | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Offline Alembic support (version-width contract mismatch) | Later | ChatGPT | Medium | 2026-08-04 (ChatGPT claim only) |
| External-effect ABA protection (no proof external version unchanged) | Later | Mixed | Hard | 2026-08-04 (ChatGPT claim only) |
| PostgreSQL contention classification (`23505`/`40P01`/`40001` not mapped to retry) | Later | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Shared service/local lifetime process lock | Skip | Mixed | Medium | 2026-08-04 (ChatGPT claim only) |
| Backup ambiguity / reservation recovery on rename-fault | Skip | Mixed | Hard | 2026-08-04 (ChatGPT claim only) — see `database-backend-postgresql-test-plan.md` §2 |
| Native/SQLite migration-target helper still asserts wrong head (`0018` vs `0028`) | Must-fix before trusting certification, not a runtime launch blocker | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Commit-before-response / lost-response replay harness | Later | Mixed | Hard | 2026-08-04 (ChatGPT claim only) — see `database-backend-postgresql-test-plan.md` §1 |

## Cat-3 repo hygiene — claimed pure repo fixes, not independently verified

| Item | Priority | Owner | Local effort | Last verified |
| --- | --- | --- | --- | --- |
| Missing semantic-invariant diagnostic mappings in `_semantic_relationship` | Later | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Admin command metadata scattered across `admin.py`/`admin_cli.py`/`application.py` | Skip | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Read-only submission/destination authority still imported from `step9` | Skip | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Delete stale `dish_pg/shadow_worker.py.orig` | Later | ChatGPT | Easy | 2026-08-04 (independently corroborated — found unprompted by a fork in this session) |
| Three missing ORM index declarations (present only in migrations) | Later | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Nested continuation still exposes internal `verify` action | Later | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| Stage 6 runbook still references `0015` instead of `0028` | Skip | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |
| `DishService` typed seams incomplete (coordinators still accept `service: Any`) | Skip | ChatGPT | Easy | 2026-08-04 (ChatGPT claim only) |

## Docs needing refresh

| Doc | Issue | Last verified |
| --- | --- | --- |
| `docs/database-backend-production-change-ledger.md` | Stale — last reviewed through `42619b9` (2026-08-01), 74 commits behind HEAD `b90f42c` (2026-08-04) as of this snapshot | 2026-08-04 (fork-verified) |
| `docs/database-backend-imp.md` | Reasonably current/self-aware; no action needed | 2026-08-04 (fork-verified) |
