# Live test-project smoke

Do not run this against production Cooking. Use disposable tasks in the configured test project and
preserve the complete JSON transcript.

## Status

Updated 2026-07-29. Stages 1 and 2 record completed work; Stage 3 is the bounded activation gate,
Stage 4 tracks focused post-activation confidence, and Stage 5 holds low-priority breadth and
hardening. The completed evidence spans several runs and run IDs, so it is not a substitute for the
final single-run rehearsal.

Saved reports:

- `/tmp/dish-admin-smoke-c381280a.txt`
- `/tmp/dish-backend-database-smoke-8b0f2b01.txt`
- `/tmp/dish-broader-smoke-e9cad9e1.txt`
- `/tmp/dish-postfix-smoke-7ab6dc94.txt`
- `/tmp/dish-stage3-135d1db2.txt`

These `/tmp` reports are working evidence, not permanent release records. Copy the final Stage 3
transcript to the approved rollout record location before relying on it for activation.

## Tracking discipline

Use this file as the current smoke-test ledger. After every live or isolated service smoke pass:

- update the applicable stage instead of maintaining a separate informal checklist;
- record the date, tested revision, run ID, fixture task IDs, and durable report/transcript paths;
- mark each gate done, partial, blocked, or requiring post-fix retest;
- add confirmed defects only after safe reproduction and state the exact passing condition;
- remove an open regression gate only after its original input and neighboring cases pass;
- record disposable fixtures and cleanup state before handoff.

Keep complete request and response bodies in the referenced transcript, with credentials redacted.
Do not turn this ledger into an incident log or paste large responses into it.

## Preconditions for every live stage

- The complete unit and hermetic SDK suites pass.
- Service host uses `DISH_HONEST_PATH=/home/marco/honest-pantry-dish-rollout`.
- Service host uses the test `DISH_COOKING_PROJECT_GID=1216693403164366`.
- Test state is isolated under `/home/marco/.local/state/dish/test/`; it does not reuse the
  production database or backup directory.
- Private Serve and public Funnel endpoints match `deploy/tailscale/README.md`.
- The service database and Asana test project have been backed up.
- `DISH_LIVE_MODE=1` and `DISH_MODE=service` are set on CLI/admin clients.
- Mint one canonical lowercase UUID `run_id` and reuse it throughout that stage.

## Stage 1 — foundation smoke: done

Completed against the live test project and isolated copies of its service database:

- Private health reported healthy database, compatibility, Asana, audit, and maintenance state.
- Credential scopes and private/Action listener separation failed closed.
- A disposable task was created through `dish create`.
- Planning completed through a real Asana write and movement to Research Queue.
- The task was reread and its exact title, notes identity, placement, operation, request, run, and
  lease evidence were confirmed.
- Successful and failed-first request replay, conflicting request-ID reuse, and replay across
  restart behaved correctly on the later backend run.
- Lease renewal preserved the lease identity; expired-lease recovery returned the correct task,
  operation, state, and released lease.
- Cold start, one-process database ownership, idle `SIGTERM`, in-flight request drain, and restart
  reconciliation behaved correctly.
- Asana-unavailable and corrupt-database modes remained diagnosable and failed closed.
- Managed restore recovered an isolated corrupt live database and restored usability.
- SQLite lock contention, abrupt process loss, concurrent starts, malformed bodies, and final
  listener/WAL/SHM settling were exercised.

Disposable live task `1216941434175836` remains in Research Queue and requires Stage 3 cleanup.

Before treating Stage 1 as an activation record, rerun and record the preconditions above. The saved
smoke reports do not prove that the complete unit/hermetic suites, Asana-project backup, or every
service-host environment value were checked in the same run.

## Stage 2 — adversarial admin and resilience smoke: post-fix regression run

Admin identifiers, authority, replay, leases, backups, restore interruption, filesystem failures,
protocol input boundaries, and database recovery were probed. Reproduce a suspected defect twice
where safe, apply no code fix during smoke testing, and rerun the affected gate after a fix is
claimed.

### Post-fix run 2026-07-28

Tested checkout `f99f46d8a8e255bf553ccff3bec7adab8bcdab4f` with run ID
`7ab6dc94-8dd6-4fd3-ae63-a62cdad1601c`. Complete redacted requests and responses are in:

- `/tmp/dish-postfix-smoke/live-http.jsonl`
- `/tmp/dish-postfix-smoke/isolated-http.jsonl`
- `/tmp/dish-postfix-smoke/cli.jsonl`
- `/tmp/dish-postfix-smoke-7ab6dc94.txt`

The following original gates now pass and remain normal regression coverage:

- undeclared arguments return field-specific `INVALID_ARGUMENT`;
- raw leading and trailing bearer-token whitespace is rejected on agent and admin scopes;
- the original interrupted restore request ID recovers successfully after restart;
- unwritable backup destinations identify the destination rather than the live database;
- read-only database health returns HTTP 503 with `write_ready:false`;
- truncated and schema-altered immutable backups are non-retryable;
- protected JSON routes require `application/json`;
- duplicate JSON keys are rejected recursively;
- duplicate Planning fields and full-document headings return actionable occurrence and line data;
- padded operation IDs return a canonical CLI result without a traceback;
- leading-zero task IDs and malformed/uppercase run IDs are rejected;
- migration lookup is no longer globally blocked by the old release asset;
- wrong-principal reads advertise no mutation action;
- terminal `recover-lease` preserves task/operation identity and has stable replay;
- successful backup and governed-authorization mutations replay exactly and conflicting reuse fails;
- semantic duplicate governed authorization returns the existing fully bound authorization.

The post-fix run confirmed these defects at that revision:

1. Every generic admin command tested returns `INTERNAL_ERROR` for a present but empty
   `arguments` object instead of identifying its missing fields.
2. Unknown-operation admin failures are unstable: `recover` returns HTTP 500 then strands the
   accepted request as `BACKEND_UNCERTAIN`, while `discard` mislabels the domain failure as database
   unavailability and does not bind exact or conflicting replay.
3. After a checkpointed restore interruption, a new request UUID naming the same backup performs a
   second restore instead of returning the recovered original result.
4. A successful restore changes the installed database mode from owner-only `0600` to `0644`.

Repeat each original failing input twice after a fix, plus partial arguments, another unknown
operation, both restore retry orderings, and normal/migrated/reconciled restore permission checks.
The interactive-shell `DISH_SERVICE_URL` still names the public listener without `:8444`; this pass
used the documented private endpoint explicitly and did not change configuration.

### Targeted DISH-002 diagnostic run 2026-07-28

Tested checkout `361b857e17f23ac0129b3180a1d7d4c18bf9193f` with run ID
`847d90c4-52b4-4f1e-b0f5-6a7cc791d9c4`.

The original safe live probe, `POST /v1/admin/discard` with unknown canonical operation
`55555555-5555-4555-8555-555555555555`, now returns replay-bound
`NOT_FOUND / operation_not_found`. It no longer enters database initialization, so the exception
lost during the earlier run cannot be recovered from that path.

An isolated service then used a directory as its database target to exercise the updated
initialization boundary without touching live state. Two fresh request IDs under the same run
produced:

- public `INTERNAL_ERROR / service_database_unavailable` with `retryable:true`;
- `error_classification: sqlite_error`, `error_type: OperationalError`,
  `sqlite_errorcode: 14`, and `sqlite_errorname: SQLITE_CANTOPEN`;
- a complete logged traceback ending in `sqlite3.OperationalError: unable to open database file`;
- allowlisted log context containing only surface, command, owner, run, request, and operation IDs.

The log did not expose the admin token or supplied reason. This confirms the DISH-002 diagnostic
boundary for a real SQLite initialization failure, but it does not identify the historical
unknown-operation exception because that trigger was fixed before logging became available.
The isolated listeners were stopped; live health remained HTTP 200 with schema 26,
`write_ready:true`, and no restore fault. Later DISH-005 and DISH-014 results are recorded below.

### Targeted closeout run 2026-07-29

The checkout advanced from `29db8cf` to `b4407e6` while testing was in progress; the final live
service ran from the clean `b4407e6` checkout. Admin run ID
`42eea002-a91d-4a30-9486-f41d42852938` was used throughout. The required independent verifier used
run ID `9380fd35-353a-45e5-bdb1-6906eb6fe352`.

No standalone complete transcript was saved, so this pass is regression evidence but not an
activation record. Temporary isolated evidence remains under
`/tmp/dish-restore-interrupt2-42eea002/`; credentials were not recorded.

The following gates pass:

- all nine generic admin routes return field-specific diagnostics for empty `arguments`, with exact
  replay;
- unknown-operation `recover` and `discard` return stable replay-bound `NOT_FOUND` twice, while
  changed request-ID reuse returns `CONFLICT`;
- DISH-005 passed two real `SIGKILL` interruptions after `replacement_committed`, in both
  original/fresh request retry orderings, without a second restore or extra pre-restore snapshot;
- normal and interrupted/reconciled restores install the database as owner-only `0600`;
- DISH-014 persisted the Verification-start attestation, rejected a different verifier run, approved
  without accepting a new attestation, replayed exactly, and rejected conflicting reuse;
- a live Small correction retained distinct reviewed, corrected, and signed identities. After a
  service restart, `sections`, `inspect`, `submit`, and final `read` remained healthy with no
  `database_semantic_evidence_invalid`;
- isolated shutdown drained an admitted request, refused a later request after `SIGTERM`, and exited
  without a stray listener or process.

One defect remains confirmed: `recover` does not validate required `outcome` and `reason` before
operation lookup. With either field absent, an unknown operation returns `NOT_FOUND` and a terminal
operation returns `WRONG_STATE`; both should return field-specific `INVALID_ARGUMENT`. This was
reproduced with separate request IDs, including stable replay on the unknown-operation cases.

Strict closeout still requires one second fresh live Small-correction fixture and the migrated-backup
restore permission check. The normal and reconciled permission cases already pass.

Disposable task `1216978477285994`, operation
`6a279005-07e9-44ab-8d03-6ddcf12fc3f0`, remains completed and `ready` in Reference and requires
approved test-project cleanup.

## Stage 3 — complete the live rehearsal: in progress

Use one new `run_id`, new disposable tasks, and one continuous transcript. Recheck Stage 2 gates
whose fixes have been claimed before relying on the workflow result.

### Connected GPT pass 2026-07-29

One disposable end-to-end Action lifecycle passed against protocol 1.0.10, task schema 2, and test
project `1216693403164366`. The checkout revision and a durable complete transcript were not
reported, so this is gate evidence rather than the final activation record.

- Primary run ID: `25dfc6b1-2f8d-4fa8-a13f-67e1b060a1b2`.
- Independent verifier run ID: `2e5ac3fe-15fa-4d22-9c77-a0a8e1f3689d`.
- Fixture task: `1216980073160976`; terminal Research operation:
  `294e046c-6167-4648-b97a-280317b15a63`.
- Read-only `sections` succeeded without a request ID and returned the complete configured
  destination set.
- Planning operation `56e44db3-18be-4375-9aae-feae139b7c94` completed and handed off to Research.
  Research then wrote one canonical candidate and moved the task once to Verification Queue.
- The independent verifier inspected exact candidate identity
  `4aaa42f240a636bafae6f7b1e9fc137e373503cd9d1fcf9e31f3f98782725809`, approved with no
  correction, signed identity
  `0401369187dc41df393c19ffb9d7d3b9f1be3e09c634e6bb80fff3b2696d18de`, and submitted once.
- Final reread showed `ready`, Reference placement `1216891250621322`, the exact signed identity,
  no drift, no migration requirement, no open operation, and no active lease.
- Exact replay of create request `0c3af1c3-3d07-4bc7-b0f8-4ace0e3d7d5c` returned the same task
  with `request_replayed:true`. Changed-title reuse returned non-retryable
  `CONFLICT / service_request_identity_conflict` and caused no second mutation.
- No connected-tool transport failure, raw exception, uncertain result, repeated movement, or
  contradictory terminal state occurred.

No defect was established by two observations: post-approval inspection reported consumed
inspection flags while coherently exposing only `submit`, and Research canonicalization replaced
the fixture prefix with the protocol-required `[non-main]` title. Retest only if either contradicts
the documented contract.

The fixture remains completed in Reference and requires approved cleanup. Connected-only breadth
not exercised in this pass is tracked in Stages 4 and 5.

### Private/admin pass 2026-07-29

Run ID `135d1db2-5f6c-4ebb-b760-42bf20c907c7` was used throughout. The checkout advanced from
`e2772d6` to `238a564` and an external service restart replaced PID `1123801` with PID `1182314`
during the pass, so this is useful gate evidence but not the required single-revision activation
record. The complete redacted final capture is `/tmp/dish-stage3-135d1db2.txt`.

Passed:

- test-project, isolated-state, Honest-path, listener, credential-scope, and private-health
  configuration checks; all four public private/admin paths returned 404;
- Action, agent, and admin tokens were accepted only on their intended surfaces;
- an expired disposable Planning lease was recovered with exact replay and changed-payload conflict,
  then the original GPT run completed Planning and produced an exact Research handoff;
- one governed `Priors` authorization persisted the operation, task, run, before/after values, and
  audit identity; exact replay passed and changed reuse conflicted;
- managed backup `dish-20260729T102249.101731Z-stage3-135d1db2-33aeffab.sqlite3` restored with the
  exact source/installed hash, schema 30, healthy readiness, no restore fault, and database mode
  `0600`; pre-backup operation, lease, and authorization facts returned while the harmless
  post-backup request disappeared;
- migration of an already-current task failed closed with `migration_not_required`;
- final private health was HTTP 200, both loopback listeners belonged to one service process, and
  no WAL or SHM file remained.

Activation blockers:

- `recover` still looks up the operation before validating required `outcome` and `reason`. Fresh
  post-restart requests returned `NOT_FOUND` and `WRONG_STATE` instead of field-specific
  `INVALID_ARGUMENT`; older requests replayed those same failures.
- The complete hermetic suite reported 904 passed and one failure. The concurrent-start lease test
  intermittently returned `INTERNAL_ERROR / legacy backup schema version mismatch` instead of
  `CONFLICT`; two focused reruns produced one pass and one reproduction.
- No disposable previous-schema task or unresolved write, movement, or execution existed. The old
  migration and interrupted-recovery paths were not fabricated through direct Asana or database
  writes and remain outstanding.
- The test-project Asana backup identifier and approved cleanup path were unavailable. Existing
  fixtures therefore remain.

Fixture `1216977588837281`, Planning operation `51acea16-fa70-4011-a45f-b34e7a8ab3b8`, is now
completed in Research Queue and requires cleanup. Existing disposable change operation
`51fa4606-5972-443e-a72e-079469b12b63` on task `1216967695177035` remains open and unleased; its
pre-existing applied workflow effects correctly prevented discard.

### Activation gate ledger

1. **Partial:** runtime configuration, endpoints, database backup, revisions, and health were
   recorded, but the checkout/service changed mid-run and no Asana backup ID was available.
2. **Passed:** the public endpoint returned 404 for `/health`,
   `/v1/commands/sections`, `/v1/admin/recover`, and `/v1/admin/backups/create`.
3. **Passed:** Action, CLI, and admin token scopes failed closed across the private and public
   listeners.
4. **Passed by connected GPT:** task creation and Planning → Research completed with exact
   identities and placement checks.
5. **Passed by connected GPT:** Research → Verification used one canonical candidate and moved
   exactly once.
6. **Passed by connected GPT:** distinct-run Verification, inspection, approval, mandatory submit,
   durable signed identity, and final Reference placement completed.
7. **Partial:** private `dish inspect` covered live, terminal, and unknown operations with structured
   identities and recovery guidance; `dish-admin` exposes no inspection verb. Wrong-state admin
   recovery returned structured task/operation identity but is affected by gate 13.
8. **Passed:** an expired disposable client lease was recovered, replayed, conflicted on changed
   reuse, and the original run completed the legal Planning continuation.
9. **Passed:** private governed-change authorization persisted exact durable binding, replayed
   exactly, and rejected conflicting reuse.
10. **Outstanding:** interrupt one disposable workflow operation at a documented recoverable
    boundary; reread before `dish-admin recover`, then compare CLI, HTTP, live Asana, and durable
    evidence. No supported live fault injector or pre-existing unresolved effect was available.
11. **Partial:** canonical no-migration behavior passed; no disposable previous-schema task existed.
12. **Passed:** a managed backup, harmless operation, restore, durable-state comparison, health,
    exact installed hash, and owner-only database permissions passed.
13. **Confirmed defect:** `recover` does not validate missing `outcome` and `reason` before
    operation lookup. Unknown and terminal operations, fresh post-restart requests, exact replay,
    and changed-payload conflict were covered.
14. **Partial:** final health, listener/process ownership, database mode, WAL/SHM settling, and
    fixture state were recorded. Approved deletion was unavailable and the fixture inventory is not
    empty.

Stage 3 deliberately does not repeat every adversarial permutation already covered in Stage 2. It
must prove that the private/admin capabilities required to diagnose and recover an activated
service work at the tested revision.

## Stage 4 — focused post-rollout confidence: partial

Stage 4 is not part of the bounded activation gate. Run it after Stage 3, or earlier in parallel
where a connected GPT can exercise Action-only cases safely. It covers the remaining cases with
meaningful value under normal concurrent use.

### Connected Action run 2026-07-29

Run ID `eeb53d49-29d0-4742-afe8-5586cdc524ec` and independent verifier run ID
`4ea1826e-4997-4a71-bf6d-07a48672dc73` were used against protocol 1.0.10, task schema 2, database
schema 30, and the configured test project. Git HEAD was
`74bebfcda19158a64f1b869276ad4b135056f381`; unrelated externally owned working-tree changes were
present, so this is live gate evidence rather than an exact-revision record. Complete credential-free
requests and responses are in
`/tmp/dish-stage4-eeb53d49-29d0-4742-afe8-5586cdc524ec.jsonl`.

Passed:

- A fresh Small-correction fixture completed Planning, Research, independent Verification, Small
  approval, mandatory submit, and final reread. Reviewed identity
  `f6066bd02288c7a9cfcc7e73dd01d8196d880970cf221311aefedd1720922860`, corrected candidate
  identity `26bd04fa1e34b4d473fab74e92e5a2f7d660a37e0aa96b3349d292cc571df708`, and signed identity
  `70640144b0679fe0b571c8f71df87a2964e28a4697a93d383dbd125c944a2950` were distinct.
- Final read showed `ready`, Reference placement `1216891250621322`, the exact signed identity, no
  open operation, no service lease, and no legal next action. No
  `database_semantic_evidence_invalid` or other backend error occurred.
- Action lease renewal preserved lease ID `685ee355-edfc-47b6-bfff-a0ff7817cbf7`, task, operation,
  owner, run, acquisition time, renewal time, and expiry on exact replay. The replay was marked
  `request_replayed:true`; changed-operation reuse returned non-retryable
  `CONFLICT / service_request_identity_conflict`. Inspection showed the original operation still
  open and unchanged with only `prepare` legal.
- An empty Planning candidate returned retryable `VALIDATION_FAILED` with every missing field
  identified. Exact replay returned the stored failure, while corrected content under the same
  request ID returned non-retryable `CONFLICT`. Inspection proved unchanged content identity,
  placement, operation phase, lease, and legal action before a fresh request completed Planning.
- Final private health was HTTP 200 with database write readiness, Honest compatibility, Asana
  readiness, and maintenance readiness all healthy. The transcript contains no credential.

Blocked by the connected-Action boundary:

- Stale content and stale placement could not be manufactured safely. The Action exposes no legal
  independent mutation that introduces either drift while preserving the original bound
  continuation. Direct Asana or private/admin mutation would invalidate this connected-surface gate.
  Retain both cases for a future supported drift fixture or fault injector rather than inferring
  behavior from wrong-state transitions.

Disposable fixture task `1216981521707211`, Planning operation
`fa195d69-b76b-4256-a25a-5f10de634ee1`, and terminal Research operation
`db5fda40-b0d0-4501-9550-acbd777d4b34` remain in the test project. The task is `ready` in Reference
and requires approved cleanup.

## Stage 5 — low-priority breadth and resilience

Stage 5 is optional post-rollout hardening. Run individual cases when real usage supplies a safe
fixture, an observed failure raises their value, or broader regression confidence is worth the
operator time. Do not delay activation for this stage.

### Connected Action breadth

- Exercise Large, Evidence, and Human Review paths.
- Exercise a movement retry only after an explicitly retry-safe or replay-safe result.
- Complete the GPT Action editor Preview gate and compare its envelope with the private CLI.
- Preserve a physically separate second-GPT transcript for independent Verification.

### Expanded admin matrix

- Exercise every exposed admin operation with its valid path, wrong state, stale or unknown
  operation, cross-run or cross-actor authority, and mismatched supplied identifiers where
  applicable.
- For every admin mutation, test exact replay, changed-payload conflict, and failed-first request
  identity binding.
- Repeat malformed and noncanonical task, operation, run, request, lease, destination, and supplied
  identifier cases.
- Cover discard, reopen variants, every private continuation, lease release or takeover, and
  destination correction permutations.
- Check durable audit persistence of task, submission, operation, run, actor, request, supplied,
  lease, destination, and before/after state fields across successful and failed mutations.

### Optional resilience

- Create an operation-backed uncertain write or movement with a supported fault injector and follow
  only the returned recovery action.
- Exercise administrative destination repair.
- Run a longer idle/request soak and confirm thread, listener, SQLite handle, WAL, and lease counts
  settle.
- Add shutdown and restart timing variants beyond the bounded Stage 3 final-state check.

## Stop conditions

Stop immediately on any raw exception, `BACKEND_UNCERTAIN`, credential appearing in output, public
access to a private route, duplicate provenance, repeated movement, or mismatch between live Asana
state and local durable evidence. Preserve the exact response and identifiers. Resume only after the
failure is resolved, then repeat the affected gate with a fresh disposable fixture and request
identity.

## Permanent-run abandonment rehearsal

Before enabling the Marco-only commands in production, rehearse each path against a disposable live task:

- expired/released untouched Planning and Research attempts create exact prepared successors without an Asana write or movement;
- a clean bound Verification attempt closes only its incomplete cycle and returns an exact successor operation/cycle target;
- an older actor lease cannot be selected after a later actor attempt exists;
- the abandoned owner/run cannot claim any returned successor or continuation;
- a pending step or uncertain effect returns a private `reconcile-abandonment` action and acquires no connected lease;
- the agent relays the exact command, waits for Marco, refreshes the authoritative action, and follows only the refreshed target;
- process loss after abandonment creation is resumed through the same operation execution and both service request IDs replay their stored results;
- a completed route-preserved Verification continuation remains exact-targeted until its cycle is claimed.

### Live rehearsal 2026-07-30

Run against the test project (`DISH_COOKING_PROJECT_GID=1216693403164366`,
`DISH_DB_PATH=/home/marco/.local/state/dish/test/shared.sqlite3`) via the already-running
`dish-service` reached through `https://laptop.tail46f0b9.ts.net:8444`. Marco explicitly authorized
running `dish`/`dish-admin` directly for this session, scoped to the test project only. Complete
credential-free request/response bodies are in `/tmp/dish-abandonment-rehearsal-2026-07-30.jsonl`.

Passed:

- **Clean Planning abandonment.** Fixture `1217037869783923`, Planning operation
  `62253077-25c3-4246-a13d-1d376b83ca70`, lease `1e3697f5-aa6b-40ce-923f-167a473b35e7` (run
  `eebfd364-2bc6-4c5e-8b8f-c7a892b4ed16`). After `expire-lease` and `abandon-operation`,
  classification returned `restart_prepared` with exact prepared successor
  `b0bfeb0a-9677-46d4-ba34-71a32552fec1`. `read` confirmed unchanged live identity, placement, and
  `modified_at` — no Asana write or movement occurred.
- **Abandoned-owner rejection.** The abandoned run (`eebfd364...`) attempting to claim the returned
  successor was refused `AGENT_MISMATCH/abandoned_run_claim_forbidden`. A fresh run
  (`6db9a0cb-fe84-4f70-a570-522c4cdf5ad4`) claimed it cleanly.
- **Clean Research (initial) abandonment.** Same fixture, Planning prepared and handed off to
  Research; Research operation `3d318753-b176-45ea-8997-6ed371f79734` (run
  `804a5580-3107-4763-8ab2-6b503863f6b2`) was released and abandoned the same way, returning
  `restart_prepared` with exact successor `4c49df37-b042-4fc2-b0d7-3f4f595c3c11`. `read` confirmed
  `identity_matches`/`placement_matches` true.
- **Older-lease rejection.** Successor `4c49df37...` was claimed by run `1d66b112...`
  (lease `716530f2-8b99-4786-bd25-e09fdd4dfa76`), that lease was expired, and a later actor attempt
  was created on the same run via `reject --route evidence` (a pre-construction Evidence hold).
  `abandon-operation` supplied the stale older lease id and was correctly refused
  `CONFLICT/abandonment_lease_not_eligible`. The hold was then resolved with admin
  `supply-evidence` to continue the rehearsal (bonus coverage of the hold-resolution path, not one
  of the eight bullets).
- **Mid-cycle Verification abandonment.** The same fixture was carried through Research completion
  and Verification start (cycle `31ea4a79-0994-4eb0-b6ee-d5c8f1abc3ca`, run
  `ceeea6e8-d6e3-422f-9a74-2a075dfb5150`). After releasing that lease, `abandon-operation` closed
  only the incomplete cycle and returned an exact successor operation/cycle target
  (`2fae553b-6d05-4e49-9daa-5453227ac901` / `eeb77beb-d660-4981-a028-8326c7ecf612`). Researcher
  lineage (`run_id 1d66b112...`) was retained on the successor; verifier identity was not. `read`
  confirmed the live task stayed in Verification Queue with unchanged identity/placement.
- **Route-preserved Verification continuation.** While `awaiting_successor_claim`, `inspect`
  repeatedly returned the same exact `target_operation_id`/`target_cycle_id`. A fresh independent
  verifier run (`194436b3-7194-4258-b75d-a93df7cca886`) claimed it successfully by supplying both
  target IDs.
- **Crash-and-resume.** Disposable fixture `1217037942022033`, Planning operation
  `b8748041-9a21-42a1-92b9-b459e26e0ce5`. `abandon-operation` was launched in the background to
  attempt an interrupted invocation, but the local call completed in well under the interception
  window (sub-50ms), so a real mid-flight kill could not be produced; instead the identical
  `abandon-operation` invocation (same submission id, lease id, and reason) was issued a second time
  immediately after the first succeeded, simulating a client that lost the response. The retry
  correctly failed closed with `WRONG_STATE/abandonment_source_not_active` rather than creating a
  second abandonment or successor chain; `read` confirmed exactly one abandonment
  (`14e1d559-64eb-41ed-a429-94470e007795`) and one successor
  (`ca4fe9d7-4d08-4f91-9e48-967269c10df1`) exist. Note: `dish-admin abandon-operation` exposes no
  `--request-id` flag (unlike `expire-lease`), so this does not exercise literal same-request-UUID
  replay — it exercises the safety property that matters (no duplicate successor chain), not the
  exact replay mechanism.

Not exercised — blocked by available tooling:

- **Blocked/uncertain-state abandonment returning `reconcile-abandonment`.** No safe way was found
  in this live rehearsal to construct a genuine pending step or unresolved external-effect attempt.
  `generic_asana_guard` fails closed against direct writes to covered Cooking tasks, so live-state
  drift cannot be manufactured through the general `asana` CLI, and local HTTP calls complete too
  fast to interrupt mid-transaction from the client side. This needs a supported fault injector (see
  Stage 5 "Optional resilience") rather than an artificial live-state construction.

Disposable fixtures remaining in the test project, awaiting approved cleanup: `1217037869783923`
(claimed successor `2fae553b-6d05-4e49-9daa-5453227ac901`, open in Verification, unresolved) and
`1217037942022033` (successor `ca4fe9d7-4d08-4f91-9e48-967269c10df1`, unclaimed
`awaiting_successor_claim`).

`recover-lease` remains the same-run path. Do not use abandonment merely because a lease expired when the original run can still return.
