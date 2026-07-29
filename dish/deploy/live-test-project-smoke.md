# Live test-project smoke

Do not run this against production Cooking. Use disposable tasks in the configured test project and
preserve the complete JSON transcript.

## Status

Updated 2026-07-29. Stages 1 and 2 record completed work; Stage 3 is the bounded activation gate,
and Stage 4 tracks post-activation breadth and hardening. The completed evidence spans several runs
and run IDs, so it is not a substitute for the final single-run rehearsal.

Saved reports:

- `/tmp/dish-admin-smoke-c381280a.txt`
- `/tmp/dish-backend-database-smoke-8b0f2b01.txt`
- `/tmp/dish-broader-smoke-e9cad9e1.txt`
- `/tmp/dish-postfix-smoke-7ab6dc94.txt`

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
not exercised in this pass is tracked in Stage 4.

### Activation gate ledger

1. **Outstanding:** record all preconditions, revisions, endpoints, database/Asana backup IDs, and
   initial health in one continuous private transcript.
2. **Outstanding:** confirm the public endpoint returns 404 for `/health`,
   `/v1/commands/sections`, `/v1/admin/recover`, and `/v1/admin/backups/create`.
3. **Partial:** the Action token succeeded on `/v1/action/sections`; CLI/admin-token rejection and
   private-route scope checks remain.
4. **Passed by connected GPT:** task creation and Planning → Research completed with exact
   identities and placement checks.
5. **Passed by connected GPT:** Research → Verification used one canonical candidate and moved
   exactly once.
6. **Passed by connected GPT:** distinct-run Verification, inspection, approval, mandatory submit,
   durable signed identity, and final Reference placement completed.
7. **Outstanding:** inspect representative live, terminal, wrong-state, and unknown operations
   through `dish-admin`; confirm structured identity, recovery guidance, and private actions.
8. **Outstanding:** expire a disposable client lease, run `dish-admin recover-lease`, and complete
   the legal recovery/continuation.
9. **Outstanding:** exercise governed-change authorization through the private HTTP-backed
   `dish-admin` client, including durable binding, exact replay, and conflicting reuse.
10. **Outstanding:** interrupt one disposable workflow operation at a documented recoverable
    boundary; reread before `dish-admin recover`, then compare CLI, HTTP, live Asana, and durable
    evidence.
11. **Outstanding:** migrate a disposable previous-schema task and confirm canonical no-migration
    behavior for an already-current task.
12. **Outstanding:** create a managed backup, perform a harmless operation, restore, and prove the
    previous operation, request, lease state, and owner-only database permissions return exactly.
13. **Outstanding regression:** after its fix is claimed, prove `recover` validates missing
    `outcome` and `reason` before operation lookup, including unknown and terminal operations,
    exact replay, and changed-payload conflict.
14. **Outstanding:** delete every disposable Asana task only through the approved cleanup path,
    then record final health, listener/process state, settled WAL and leases, and an empty fixture
    inventory.

Stage 3 deliberately does not repeat every adversarial permutation already covered in Stage 2. It
must prove that the private/admin capabilities required to diagnose and recover an activated
service work at the tested revision.

## Stage 4 — breadth and hardening

Stage 4 is not part of the bounded activation gate. Run it after Stage 3, or earlier in parallel
where a connected GPT can exercise Action-only cases safely.

### Connected Action breadth

- Attempt stale content and stale placement separately and prove zero mutation.
- Run a second fresh Small-correction fixture and prove distinct reviewed, corrected, and signed
  identities.
- Exercise Large, Evidence, and Human Review paths.
- Exercise lease-renewal exact replay and conflicting reuse.
- Bind an expected validation failure, replay it exactly, then prove corrected reuse of the same
  request ID conflicts without mutation.
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
