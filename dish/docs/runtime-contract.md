# Dish runtime contract reference

Command syntax and invocation live in `dish --help` / `dish <stage> --help` / `dish-admin --help`,
setup lives in `dish/README.md`, and internal design lives in [architecture index](architecture/index.md).
Always invoke the `dish`/`dish-admin` wrapper scripts, not `python -m dish_tool...`; the latter does
not run the CLI entrypoint.
This document is the reference for what a response actually means once you've made a call: the JSON
envelope shape, exit-status handling, and recovery.

## Authority and scope

The live Asana Cooking task is authoritative for title, body, workflow state, provenance, and cooking instructions. Agents access protocol-managed Cooking tasks only through `dish`; they do not read or write those tasks through the generic Asana CLI. Planning's read-only lookup of completed cooking history through the generic `asana` CLI is the one deliberate exception. It does not authorize writes to governed tasks.

The `ai-tools` checkout supplies deterministic validation and the client executables. In live multi-agent mode, one laptop-hosted `dish-service` process is the sole writable authority for operation state, leases, Asana credentials, audit/recovery, backup, and all governed task mutations. A repository copy or copied SQLite database is never a cross-agent lock.
The single-agent local test path remains available only for controlled development and is not live multi-agent authority. It requires explicit `DISH_MODE=local` and a separate database that has never been marked as service-owned. Service mode, local `dish`, and local `dish-admin` acquire the same canonical exclusive OS process lock before opening the governed database for mutation and hold it for the full database/process lifetime. The persistent service-owned marker is durable policy evidence, parent-directory fsynced after replacement, and is not the concurrency primitive.

Candidate files are ephemeral complete-text inputs. In service mode the client reads the file and sends its text; the server never opens a client filesystem path. The live task is reread before mutation and after every write or move. Do not edit a candidate after recording the identity supplied to Verification.

## Access-path contract

| Caller | Network path | Credential | Permitted surface |
|---|---|---|---|
| `dish` CLI | private Tailscale Serve/tailnet endpoint | agent CLI bearer token | bounded agent commands and lease renewal |
| `dish-admin` | private Tailscale Serve/tailnet endpoint | separate environment admin bearer token | test administration for agents; production administration for Marco |
| GPT Action | public Tailscale Funnel endpoint on its own HTTPS port; root is production and `/test` is TEST | separate environment Action bearer token | `/v1/action/*` commands and Action lease renewal only |
| local tests | direct local application mode | local Asana test credential when required | controlled single-agent development only |

Asana ABA protection is fail-closed. `DISH_ASANA_MODIFIED_AT_RELIABLE_EFFECTS` is unset by default and accepts only a comma-separated subset of `content,movement,completion`. Enable each class only after local verification against the deployed Asana workspace/API proves that every governed mutation in that class advances the observed `modified_at`, including rapid mutate-then-revert and read-after-write observations. An uncertified class cannot produce `not_applied` from a returned-to-baseline state.

Live client environments set all of:

```text
DISH_LIVE_MODE=1
DISH_MODE=service
DISH_PROFILE=prod
DISH_SERVICE_URL_TEST=<test private service URL>
DISH_SERVICE_URL_PROD=<production private service URL>
DISH_CLIENT_RUN_ID=<non-nil canonical lowercase UUID for this run>
```

The CLI uses `DISH_SERVICE_TOKEN_TEST` or `DISH_SERVICE_TOKEN_PROD` with the matching named profile.
Interactive agent shells receive both environment-specific admin tokens, but agents may use only
`DISH_ADMIN_TOKEN_TEST`; production administration remains Marco-only. The `--profile` flag selects
one invocation, `DISH_PROFILE` supplies the process default, and production is the fallback default.
A named profile never falls back to a generic token. Each environment's GPT Action stores only its
matching `DISH_SERVICE_ACTION_TOKEN`; fixed Caddy paths are independent of private client profiles. No client
receives the service database path or Asana credential.

Environment selection follows intent, not caution: genuine Dish work uses production. Test is only
for experiments, rehearsals, destructive testing, or Marco's explicit request. An agent must not
redirect real work to test merely because test feels safer. If the intended environment is
ambiguous before a mutation, the agent must stop and confirm it with Marco.

The service host is the only place that defines `ASANA_PAT` or `ASANA_ENV`. Each environment runs
one process, enforced by a host file lock tied to that environment's shared database. Each process
exposes two loopback listeners:

- private CLI/admin listener, intended for Tailscale Serve;
- Action listener, intended for Tailscale Funnel.

Both listeners also serve the unauthenticated, read-only generated Action schema at
`GET /openapi/action.json`; that document is outside the bearer-token permission surface. The public
listener does not route private CLI, admin, health, migration, recovery, or backup endpoints. HTTP
status remains transport information; workflow meaning remains in the canonical JSON result code.
On the GPT Action surface, expected authenticated Dish rule outcomes (including `INVALID_ARGUMENT`,
state conflicts, and validation failures) use HTTP 200 so the Action runtime returns the canonical
envelope to the agent instead of reclassifying it as a transport failure. Authentication and
authorization failures retain HTTP 401/403, and unexpected server failures retain HTTP 500.
Protected POST routes authenticate first and then require exactly one `application/json` media type;
parameters such as `charset=utf-8` are allowed. Missing, ambiguous, or different media types fail
before JSON parsing. JSON objects with duplicate keys are rejected recursively before client identity,
request replay, or mutation. Private media-type failures use HTTP 415; authenticated Action failures
remain canonical HTTP-200 Dish envelopes.

SIGTERM and SIGINT close one shared admission gate before listener teardown. Requests that have not
crossed that gate are disconnected before authentication, body parsing, replay journaling, database
access, or workflow execution; callers may therefore observe an ordinary transport-unavailable
failure during restart. Requests already inside dispatch drain normally. Every Dish response includes
`Connection: close`, so a reverse proxy cannot reuse a pre-shutdown loopback connection to admit new
work while systemd is waiting for the active handlers to finish.

Agent-facing action guidance is authoritative even on failures. When an operation-scoped command is
rejected, `allowed_actions` reports the currently legal exposed continuation when one exists. A
retryable candidate-validation failure therefore keeps the same corrective command available.
Fresh bare tasks created by `create` report `data.required_start_kind: planning`. A connected
Planning start is always a two-request operation-intent exchange before ordinary workflow admission.
The first `start` with `kind=planning` and a new `client.request_id` returns
`CONFIRMATION_REQUIRED` with `data.intent_challenge_id`; it does not read or change the task, create
an operation, or acquire a lease. Supplying `intent_basis` on that first request does not bypass the
challenge. To continue, make a fresh call with a different `client.request_id`, the exact same
Planning target and agent, the returned `intent_challenge_id`, and either
`intent_basis: user_requested`, or `intent_basis: agent_override` with a non-blank
`override_reason`. Use `user_requested` only when Marco actually requested Planning for that exact
task. The challenge is bound to the authenticated owner/run and is single-use. Exact replay of the
first request returns the same challenge; exact replay of the confirmed request converges on the same
operation and result. If confirmed admission then reports a state or admin prerequisite, satisfy that
prerequisite and begin a new two-request Planning confirmation exchange rather than reusing the
claimed challenge.

An Asana-completed bare task cannot start Planning directly after confirmation: the confirmed
`start --kind planning` returns
`planning_completed_task_reopen_required`, `data.required_admin_action: reopen-planning`,
`data.resolver: Marco/admin reopen-planning`, and `data.legal_next_step` directing Marco/admin to
run that audited command with a reason and, only after success, directing the agent to retry
`start` with `kind=planning` and a fresh `client.request_id`. Marco's private `reopen-planning`
command is the only route that clears the completion flag; it preserves exact
content and placement, persists a completion-state attempt, and records both domain and invocation
audits before exposing `start` again. A completed cross-stage handoff reports `start` and the required
start kind even though the old operation itself is terminal. If a caller nevertheless requests
Planning again against the valid Planning brief, Dish returns
`planning_handoff_requires_initial`, keeps `start` exposed, and repeats
`data.required_start_kind: initial`; this is not a completed-task reopen state. Verification
`start` exposes only `inspect` after the review binding is complete. The exact
verifier run must then inspect the still-current candidate in Verification Queue; that reread appends
a cycle-bound `dish_inspect` fact and only then exposes `approve` and `reject`. Task-level `read` responses expose any active operation, its submission
ID, workflow state, and principal-filtered next actions. With no active operation, `read` derives the
resting-task continuation from the same authority used by `start`: a bare task may start Planning,
a valid Planning/canonical Research baseline may start Initial Research, and an exact current-schema
`ready` identity with durable local signoff lineage may start `kind=change`. A merely `ready`-looking
task without that exact signoff exposes no ordinary Change action. Successful operation-scoped lease renewal
includes both `task_gid` and `submission_id`; renewal of a terminal operation reports
`WRONG_STATE / operation_not_open` with the terminal status.

## Service ownership and leases

The durable `operations` constraint is the one-active-operation-per-task lock. A separate durable `operation_execution_claims` row serializes mutation execution for that operation, and partial unique indexes prohibit multiple unresolved writes or movements for it. Service mode and both local mutable entry points acquire incompatible exclusive forms of one OS process lock derived from the canonical database target before opening SQLite and retain it for their complete database/process lifetime. The service also writes a persistent ownership sidecar derived from that target; the sidecar is policy evidence, not the lock, and direct local CLI/admin mode will not open a service-owned database through a symlink alias even while the service process is stopped. `service_leases` bind the current actor to an owner identity and run identity with a renewable expiry. Workflow handoff may release the actor lease, but it does not release the task operation lock. `allowed_actions` is principal-aware and every exposed current-action projection is filtered together: top-level `allowed_actions`, `data.legal_next_actions`, and any nested `authoritative_view.legal_actions` agree. Read-only task inspection never mutates lease state.

Expired/inactive ownership has two ordinary continuations. The **same durable run** receives no current mutation and is directed to private `dish-admin recover-lease`; that releases stale lease liveness without transferring workflow ownership, and actions that may become legal afterward are separated under `data.after_recovery.legal_actions`. A **different run** is offered connected Action `safe-reclaim` only when one shared mechanical predicate proves a clean restart frontier: no running/pending/uncertain consequential execution or request, no unresolved external effect, no incomplete workflow step or semantic proposal/application, no later lease/claim, no active abandonment, and an exact confirmed live baseline at a restartable Planning/Research/Verification frontier. The response includes the exact source operation and lease in `data.agent_action`; a different run never receives `recover-lease` as ownership transfer.

`safe-reclaim` is replay-bound. Its initial eligibility result is advisory; before committing, Dish enters the SQLite writer transaction and reruns the complete mechanical predicate against the exact source lease, including stage/frontier, Verification-cycle and lease-context facts, unsettled work/effects, confirmed baseline, and a fresh live task identity/placement read. A failed commit-time check rolls back before the source is terminalized. Only after that pass does Dish fence the source as terminal `safe_reclaimed`, close an interrupted Verification cycle with the same non-signing outcome when applicable, release the old lease, clone the exact confirmed successor baseline and still-valid durable authority/evidence needed by the continuation, record `safe_reclaims` lineage, and create one prepared linked successor. The replaced run is forbidden from claiming that successor. A later fresh `start` claims the prepared successor; the source remains immutable. If the clean predicate does not pass, Dish does not offer safe reclaim. Unresolved execution/effect evidence is directed through deterministic `recover --outcome inspect`/settlement first; formal `abandon-operation` remains for genuinely unsafe or uncertain dead-run recovery rather than routine clean lease expiry. Asana and SQLite cannot share a transaction, so an external Asana edit can still occur after the final live read returns but before the SQLite commit completes; this is the remaining unavoidable cross-store race, and successor claim remains fail-closed on live drift rather than treating the prepared baseline as current authority.

`dish-admin recover-lease`, connected `safe-reclaim`, and `dish-admin abandon-operation` are deliberately different. Recovery resumes the same durably bound run. Safe reclaim transfers only mechanically safe agent execution ownership to a different run through a new linked operation. Permanent abandonment names the latest classified actor lease and asserts that its owner/run will not return, then uses exact durable/live evidence to select one bounded recovery result. None of these paths performs speculative compensation across partial or uncertain effects.

If a prepared Planning/Research successor's live content or section changes before claim, `start` returns `prepared_successor_drift` only after durably changing the abandonment to `blocked_manual_reconciliation`. Marco runs the returned `reconcile-abandonment` command; Dish restores the immutable successor baseline and expected placement using successor-owned journaled effects, then the agent refreshes the authoritative action and retries the exact prepared start. A corrupt baseline binding or contradictory effect remains blocked and is never silently rebased.

The same original Initial Research run may also reacquire a missing lease when retrying a
pre-construction Evidence or Human Review `reject`. That command is still Research stage-actor work,
so the replacement lease has no Verification cycle context. A fresh run cannot use this route to
take over the operation.

An abandonment result carries `data.abandonment` and, when work remains, `data.required_action`. A connected continuation names the exact `prepared_operation_id` for Planning/Research or exact `target_operation_id` and `target_cycle_id` for Verification. A blocked result carries `surface: private-admin`, the executable `dish-admin reconcile-abandonment ABANDONMENT_ID` command, relay text telling the agent to wait for Marco, and an `after_success` instruction to refresh the authoritative action. A preserved hold that needs human-authored `--detail` returns an `admin_command_template`, and the relay text includes that template plus an explicit instruction to replace the marked detail before running it. The exact abandoned owner/run is forbidden from claiming any replacement. While the abandonment is active, the task is fenced from unrelated connected mutation and lease acquisition, including an ordinary new `start` before it can create another operation.

A crashed `abandon-operation`/`reconcile-abandonment` invocation keeps one exact operation execution as replay authority. Before workflow settlement it is named by the abandonment record. After workflow settlement clears `current_execution_id`, an unfinished execution claim still identifies the same authority. Reconciliation reclaims that same dead execution, returns the stored workflow result without repeating it, and settles the earlier service request as well as the current one; it does not create an unresolved A1/A2 execution chain. Exact request replay returns the stored result.

A prepared Planning or Research successor contains no stage output and is still unowned. If a newer
deployment changes the schema version before claim, Dish reruns the ordinary current-release live
validation and, only after it succeeds, adopts the current schema version atomically with the exact
prepared claim. An already-claimed or ordinary operation never receives this rebind.

`dish-admin expire-lease TARGET --reason TEXT [--request-id UUID]` is a separate private, database-only lease authority. `TARGET` is a canonical lease UUID, an Asana task GID, or one of the two operator URL forms `https://app.asana.com/0/PROJECT_GID/TASK_GID` and `https://app.asana.com/1/WORKSPACE_GID/project/PROJECT_GID/task/TASK_GID`. It releases the selected active row even before natural expiry, but never changes workflow state, actor lineage, execution claims, or external-attempt evidence. A live execution claim, as determined by the existing process-identity helper, returns replay-bound `CONFLICT / operation_mutation_in_progress`; a dead claim remains stored and does not block release. An already-released exact lease and a task with no active lease are successful no-ops; an unknown exact lease UUID is `NOT_FOUND`. The operation may be terminal.

Lease expiry is not durable owner/run revocation. Agents authenticate mutations by owner/run identity and may automatically acquire a missing lease, so the previous run may reacquire if lineage still permits it and no replacement lease exists.

A terminal lease is released only after the operation is terminal and every declared step and
ambiguous write/movement attempt has a durable completion outcome. Between workflow commit and that
response cleanup, or after a crash in that window, a complete terminal operation may retain a
non-authoritative cleanup-tail lease; a later task lease acquisition can reap it safely. A terminal
lease with pending steps or unresolved effects remains invalid. Lease acquire, renew, release, and
terminal checks are transactional. Configuration requires the lease TTL to exceed one
maximum-duration Asana SDK call plus the recovery safety margin; the default TTL is substantially
longer. This validation is not a whole-command deadline: one Dish command may perform several
sequential Asana calls. If post-success lease or owned-resource cleanup fails after the governed
mutation committed, the original command still returns success. A safe fallback may release the
lease and report `service_cleanup_warning`; otherwise `service_recovery_required` suppresses
follow-on actions and explicitly tells the client not to retry the mutation. Ordinary full-state
write and approval retries remain naturally idempotent by exact live-state comparison. In service
mode, all agent mutations are replay-bound, and every externally callable agent, administrative,
lease, and backup mutation requires a client-generated non-nil UUID `client.request_id`; reads (`sections`,
`section-tasks`, `read`, `proposals`, and `health`) do not. This includes `create`, `inspect`, `start`, `prepare`, `approve`, `reject`, `submit`,
`apply-proposal`, and `safe-reclaim`. Reuse that UUID only when retrying the exact same logical call
after a lost response.

## Request replay contract

In service mode, the first authoritative outcome of every agent, administrative, lease,
backup-create, or backup-restore mutation is durably bound to `client.request_id`. Request identity
includes the command, canonical arguments, authenticated owner identity, and run identity. The replay-bound mutation inventory is exhaustive:

| Surface | Replay-bound mutations |
|---|---|
| Agent Action/private CLI | `create`, `inspect`, `start`, `prepare`, `approve`, `reject`, `submit`, `apply-proposal`, `safe-reclaim` |
| Marco admin workflow | `attention`, `inspect`, `holds`, `review-queue`, `review-inspect`, `review-approve`, `review-reject`, `migrate`, `reopen`, `recover`, `repair-destination`, `supply-evidence`, `record-human-decision`, `authorize-governed-change`, `discard`, `abandon-operation`, `reconcile-abandonment` |
| Lease lifecycle | private agent lease renewal; Action `renew-lease`; Marco-admin `recover-lease` and `expire-lease` |
| Backup lifecycle | `backup-create`, `backup-restore` |

No mutation endpoint is exempt from request identity. Agent `inspect` is also replay-bound because it records durable Verification evidence. Read-only `sections`, `section-tasks`, `read`, `proposals`, and `health` do not create replay records and do not accept a request ID as mutation authority.
The connected `renew-lease` Action uses the common body shape: `arguments.operation_id` is replay-bound
alongside `client.run_id` and `client.request_id`; it is not supplied as a top-level or path parameter.

- Missing or malformed request IDs are rejected before a request record exists. Once the UUID is accepted, expected argument, state, authorization, and workflow failures are stored just like successes.
- `reject.reason` is validated after that request record begins but before backend construction, lease mutation, operation execution, evidence insertion, or task write. An unsafe reason therefore completes the request with a stored validation failure; exact replay returns that same failure, while a fresh request UUID with a valid reason may proceed.
- For `expire-lease`, malformed JSON/body shape, unexpected top-level fields, and invalid client identity fail before journaling. Once principal, run ID, and request ID are valid, exactly-one-target, target-identifier, and reason validation failures are journaled against the raw supplied target/reason. The valid mutation path trims the reason once before hashing and stores that same normalized value as `admin expiry: <reason>`. Correcting a stored validation failure requires a fresh request ID.
- A repeated completed request returns the original stored result with `data.request_replayed: true` and `data.request_id`; the first response is not labelled as a replay.
- Reusing an ID for a different command, owner/run, or arguments returns non-retryable `CONFLICT` with `service_request_identity_conflict`.
- A matching pending or uncertain request is never blindly executed again. Request completion is first-writer-wins: if an original executor and a recovery caller race to persist different envelopes, both callers return the one stored outcome, and the losing response is marked with `data.request_completion_race_resolved=true`. `start` may be reconciled only when exact durable operation and live-state evidence proves the original result. `reopen-planning` may resume the original external call only when persisted pre-effect identity, section, completion state, and exact baseline version still match and that version source is deployment-certified for completion effects; otherwise it confirms an already-applied update without repeating it or remains uncertain. Writes and movements use the same ABA rule: `not_applied` requires exact baseline state plus reliable unchanged version evidence. A returned-to-baseline state after a version advance, or a baseline match without reliable version evidence, remains uncertain in immediate, restart, and manual recovery. For multi-step workflow mutations routed through the current operation service, an active execution claim remains pending; a dead execution is reconstructed from its request-scoped durable baseline and exact changed attempts, content versions, Verification cycles, workflow steps, actor facts, and operation state. A claim-free unresolved uncertain execution still fences every fresh governed mutation on that operation.
- Exact replay of an uncertain governed decision may resume only when durable intent and exact local or external evidence prove the missing suffix. Recovery inserts or verifies the decision-specific audit exactly once, stores monotonic resolution evidence, completes the original execution and service request, and thereafter replays the resolved authoritative result. Ambiguous evidence remains uncertain and fenced; phase alone never proves a decision.
- Reconstructed partial-effect failures return non-retryable `BACKEND_UNCERTAIN` with `write_committed`, `move_committed`, `cycle_created`, `failed_step`, `authoritative_task_identity`, `required_admin_action: recover`, `required_admin_outcome`, and `safe_to_retry: false`. These values are evidence-backed and stable across restart. Confirmed writes or movements are never repeated by recovery. When a pre-construction Research hold fails inside its atomic local unit before any workflow effect commits, Dish instead returns retryable `BACKEND_UNCERTAIN` with `operation_exact_replay_required`, `request_replay_required: true`, and `required_next_action: retry_exact_request`. Only the same request UUID may resume that execution; successful replay commits the hold step and governed audit together and stores the resolved held result in the request ledger.
- A completed `submit` is replayed from the request ledger. A fresh request ID for the same already-completed logical submission is also satisfied from exact signed-content and destination-movement evidence, without reacquiring a lease or repeating the external movement.
- Ordinary request records live in `service_requests` and survive service restart. `backup-restore` uses an atomic sibling sidecar journal because the restore replaces the database that contains ordinary request records. The sidecar binds the request to the selected backup, monotonic exact-effect checkpoints, and terminal result across replacement and restart. Checkpoints identify the source bytes, prepared candidate, exact pre-restore destination, pre-replacement live files, installed bytes, validation, and rollback when applicable. A restarted restore resumes or reconstructs only when those durable identities match; a legacy pending row without a checkpoint remains non-retryable and is never inferred or blindly repeated.

The bundled CLI and admin clients generate an ID internally for most first consequential calls.
`dish inspect` and `dish apply-proposal` accept `--request-id` for exact ambiguous-response replay.
If either bundled client loses the transport response or receives an empty body, unreadable UTF-8,
invalid JSON, or a result that fails the canonical envelope contract when dispatch may already
have begun, it returns the same local non-retryable
`BACKEND_UNCERTAIN / service_response_ambiguous` envelope
containing the exact transmitted request ID and client run ID. The CLI also returns
`data.replay_argv`, `data.replay_environment`, and `data.replay_command`, which repeat the original
invocation with that same request and run identity. The client does not retry automatically. Run
only that exact replay under the same authenticated principal and `DISH_CLIENT_RUN_ID`; do not issue
an ordinary fresh retry. Most other command-line interfaces neither accept a request ID nor expose
the generated value after an ambiguous response. Those callers must inspect live and durable state
instead of blindly rerunning the mutation.
`dish-admin expire-lease` is the explicit exception: it accepts `--request-id`, prints both request ID
and `DISH_CLIENT_RUN_ID` to flushed stderr before dispatch, and requires the same authenticated admin
principal, run ID, request ID, normalized target, and trimmed reason for exact replay. Marco must
retain that run ID until an ambiguous call is resolved. GPT Action calls supply request identity
explicitly through the imported schema. The public schema marks both `client.request_id` and
`client.run_id` as non-nil canonical lowercase UUIDs; `run_id` remains stable for the whole agent run.

## Health, backup, and startup

`GET /health` exists only on the private listener and represents mutation readiness. It checks:

- current SQLite schema, semantic evidence, and rollback-only write readiness;
- exact Honest protocol/task-schema compatibility;
- Asana access and required Cooking section registry;
- pending invocation-audit repairs;
- active operations and active/expired leases.

The database probe takes a bounded write lock, updates the schema ledger only inside a savepoint, and
rolls the probe back before returning. It leaves no durable workflow or request-journal state. Health
reports `database.write_ready: true` only after that probe succeeds; read-only storage is unhealthy,
while transient writer contention remains a lock condition rather than corruption.

At startup the service validates the database, including semantic impossibilities such as duplicate unresolved attempts or a terminal lease whose workflow steps or external effects remain incomplete, resolves Honest compatibility, and replays pending invocation-audit repairs. It also discovers Planning reopen attempts whose outcome or original request is unresolved. Those rows remain semantically valid and block only their task: startup completes already-terminal attempt/request pairs, classifies safe-to-resume or already-applied live state without issuing an update, and reports exact replay or manual-authority guidance under `startup.planning_reopen_recovery`. Repair workers claim each repair transactionally so concurrent workers cannot emit duplicate events. Emergency JSONL repair writers and importers share an exclusive sidecar lock; import first atomically claims the current file, so an append that begins during import is preserved for the next pass rather than overwritten or deleted. Durable service-request records and any restore-fault marker survive process restart. Listener readiness depends on valid service configuration, not healthy external or recoverable data dependencies. An Asana outage, an invalid live database, compatibility failure, or a restore-fault marker therefore leaves the private service available for health and administrative diagnosis or restore; health remains unhealthy and workflow mutations fail before entering application mutation code. Semantic-evidence failures retain `VALIDATION_FAILED` and `database_semantic_evidence_invalid`; they are never flattened into database availability. Before execution they report that no request ID was consumed and may be retried with the same ID after repair. When discovered after a request journal begins, they report execution and request-consumption state explicitly; a consumed request ID replays its stored failure, so repair must be followed by a fresh request UUID. Backup creation and lease renewal remain available only when their own database prerequisites are healthy.

`dish-admin backup-create` produces a managed SQLite snapshot using the online backup API, including committed WAL state, and validates the complete current database contract. For a request-scoped creation, the exact managed backup identifier is committed before snapshotting can begin. The reservation then closes as `not_applied` only for a proven pre-rename/absent-destination outcome, as `confirmed` only after the exact destination validates and its parent directory is fsynced, or as `uncertain` when rename or post-rename durability cannot be proven. Replay and startup reconcile that exact reserved destination before returning an earlier failure: a valid durable file completes the original request, absence closes it as not-applied, and invalid or non-durable evidence stays uncertain. Existing unresolved reservations use the same supported reconciliation path. The validated file metadata and replayable request result commit without allocating another destination. If execution is interrupted after the file becomes durable, the same request UUID validates and completes that one reserved backup instead of creating another. Legacy pre-v3 quarantine backups use the same online-backup rule and replace a previously invalid legacy artifact rather than treating its filename as proof. `dish-admin backup-restore` accepts only a managed backup identifier, copies it into a temporary candidate, migrates the candidate to the current schema, validates it, and replaces the database atomically without modifying the source backup. It attempts a validated pre-restore snapshot when the live database is readable, but a corrupt or semantically invalid live database does not prevent restore. Restore is exclusive against ordinary requests; ordinary requests remain concurrent outside restore. Managed backup identifiers cannot resolve through symlinks outside the backup directory. Restore durably pre-arms its fault marker before database replacement; if the marker cannot be persisted, replacement does not start. The restore request journal is fsynced outside the replaceable database and records monotonic checkpoints before or after each relevant effect. After `SIGKILL`, service startup (or the next administrative restore call if startup has not reconciled it) reconciles the marker and exact journal: work before replacement resumes from the same candidate and pre-restore destination, installed candidate bytes are validated and returned without a second swap, and an interrupted rollback resumes from its exact candidate. A client retry that generated a new request UUID receives the successfully recovered original result instead of initiating the same restore twice. The terminal result is journaled before the lockout marker is cleared, so a kill in that final window replays the committed result and removes only the stale marker. Directory entries for snapshots, database replacement, journal records, and marker removal are fsynced. A failed restore reports whether rollback was actually proven. If automatic rollback cannot be proven, the marker remains beside the database; health remains unhealthy and workflow mutations stay disabled across restart until manual recovery or a successful validated restore clears it. Deterministically invalid managed backups are immutable bad inputs and return non-retryable validation failures; managed-backup destination failures are reported against the backup directory rather than the live database. Successful restore metadata names `restored.source_backup_id` and `restored.source_schema_version` separately from `restored.installed_database`, whose hash, size, and schema version describe the bytes actually installed after migration. There is no ambiguous `restored.backup_id` alias.


Admin recovery remains specific rather than generic:

- `recover-lease` releases only an expired actor lease; it never assigns workflow ownership to the admin caller;
- `expire-lease` releases the exact selected lease or the active lease for a task without changing workflow state. It is not durable run revocation, preserves execution claims, and blocks while `process_identity_is_live` returns true;
- `recover` reconciles ambiguous backend evidence by live reread. It may execute under the
  originating live actor lease only for the exact uncertain execution that advertised recovery. If
  successful recovery durably reaches `await_verification`, `held_evidence`, or `held_human`, Dish
  releases only that exact pre-existing lease after rechecking the resolved execution and complete
  local evidence, so the returned handoff action is immediately executable. It never transfers the
  lease or releases a replacement lease;
- `repair-destination` changes only the canonical Planning destination after an unrecoverable final movement failure, preserving the original approval and creating linked repair evidence for a later movement-only `submit`;
- `discard` cancels only a provably unapplied operation;
- `supply-evidence`, `record-human-decision`, and `reopen` retain their existing protocol meanings; an unanswered Verification Human Review escalation may also be dismissed with `review-reject`, which records the dismissal reason without creating a Marco decision or governed authorization and always returns the unchanged candidate to fresh Verification, irrespective of the escalation's requested downstream resume status.

There is intentionally no general-purpose `unblock` mutation for workflow state.

## Transport and external scope boundary

HTTP bodies are executed only when exactly the declared `Content-Length` bytes were received; a short body is rejected before JSON parsing. Tokens reject surrounding whitespace, numeric timeouts must be finite and positive, and private lease/backup routes reject undeclared fields just like Action/admin routes. The CLI/admin client bounds connect and response waits independently (`connect_timeout`, default 10s; `response_timeout`, default 600s, since one command can legitimately span several sequential 40s Asana calls) and closes its connection after the response. Ordinary client calls map transport failures to structured service-unavailable results and reject noncanonical responses. Consequential `inspect` and `apply-proposal` calls conservatively map transport loss, empty bodies, unreadable UTF-8, invalid JSON, and any canonical result-validation failure when dispatch may already have begun to local non-retryable `BACKEND_UNCERTAIN / service_response_ambiguous` with the exact transmitted request and run IDs, `safe_to_retry: false`, and `required_next_action: retry_exact_request`; exact replay is manual and the client never dispatches a second request automatically. `expire-lease` uses stricter phase-aware transport handling: DNS/TCP/TLS or socket-setup failure before dispatch is ordinary `BACKEND_REJECTED / service_unavailable`; timeout, disconnect, truncated body, invalid JSON, or noncanonical output after dispatch may have begun returns local non-retryable `BACKEND_UNCERTAIN / service_response_ambiguous` with the original request and run IDs and `required_next_action: retry_exact_request`. These local envelopes are not journaled; exact replay obtains the authoritative stored service result.

Every complete task reread reasserts Cooking-project membership, so a task removed between the initial scope check and the authoritative read cannot open or continue an operation with a null placement. Asana section enumeration follows all pages. Verification start requires a non-blank, single-line independence attestation. CR, LF, tabs, ASCII controls, Unicode format characters, surrogates, line separators, and paragraph separators are rejected before request journaling, operation execution, Verification-cycle mutation, or attestation persistence; ordinary Unicode text remains valid. The same validator applies to every public route that accepts an attestation. Approval repeats only the exact verifier agent/run and inherits the exact persisted start attestation; its public shape does not accept the field. Every rejection route — Large, Evidence, and Human Review — repeats the exact verifier run and inherits that persisted attestation the same way; none of their public route shapes accept the field. Actor facts are scoped to an operation, allowing a run to participate legally in a later operation without rewriting earlier lineage. Within one operation, verifier facts are idempotent per Verification cycle: the same still-live independent verifier may acquire a later-cycle fact when resuming an unchanged candidate after a deliberate hold, while constructor/material-editor independence remains enforced separately.

Rejection reasons are NFC-normalized and must remain one safe Material-change field. The boundary rejects every Unicode control, format, surrogate, line-separator, or paragraph-separator character, including CR, LF, CRLF, NEL, vertical controls, zero-width format characters, U+2028, and U+2029. It also rejects the Material-change field delimiter (`—`). Valid long single-line Unicode text remains accepted. The same validator is applied again by Material-change rendering so caller text cannot alter the seven-field, one-record-per-line grammar even if an internal caller bypasses the public command path.

## Exact external-effect contract

Every task write and movement follows one contract:

1. reread the complete live task and reassert Cooking-project membership;
2. compare exact content identity and expected Cooking-project section;
3. persist the immutable intended effect;
4. call the generated Asana SDK with automatic retries disabled;
5. reread the complete live task;
6. classify the effect as `confirmed`, `not_applied`, or `uncertain`;
7. atomically finalize its durable evidence.

An empty or incomplete SDK response never proves success; the live reread does. A confirmed
unchanged reread proves only non-application, not that a non-retryable backend rejection has become
retryable. Recovery may compare live state only with the exact persisted expected/intended evidence.
It must not reconstruct intent from a later task state, repeat a confirmed effect, or invent
Verification signoff.

## JSON response contract

Every governed CLI or admin command writes exactly one compact canonical result envelope to stdout.
Help and stage walkthroughs are documentation output; the HTTP health and OpenAPI endpoints return
their own JSON documents rather than this envelope:

```json
{
  "ok": true,
  "command": "read",
  "code": "OK",
  "task_gid": "...",
  "submission_id": null,
  "state": null,
  "retryable": false,
  "allowed_actions": [],
  "data": {},
  "errors": []
}
```

- `task_gid` identifies the Asana task when known.
- `submission_id` is the current operation identifier; the field name is retained for client compatibility.
- `state` is tool operation state, not protocol readiness.
- `allowed_actions` is the bounded next tool action list derived from the same authoritative snapshot that mutation commands enforce.
- `data` contains command-specific exact identities, diagnostics, protocol text, or completion facts.
  For `inspect`, `data.content.operation_baseline_identity` is the immutable identity captured when the operation started, while `confirmed_identity` is the latest Dish-confirmed task head. The current comparison is explicit: `live_identity` is compared with `required_identity`, and `identity_matches` reports only that comparison.
- `errors` contains structured findings with a `rule` and any supporting fields. Deterministic
  document findings include `current`: the exact submitted value when one clean value exists, or
  `null` when no single value is meaningful.

Internal workflow permission `verify` is exposed as Action/CLI command `start`. When `start` is the
required handoff command, `data.required_start_kind` identifies its required `kind`: `initial` after
Planning, `change` for an exact signed resting `ready` task, and `verification` after Research. Clients
must not look for a separate `verify` Action.

Verification `start` and `inspect` expose `data.verification_lineage`. `candidate_runs` lists the
constructor and material-editor run facts that contribute to verifier independence enforcement.
`current_run` reports the authenticated caller run, whether it is eligible to verify, and the exact
disqualifying role/rule when it is not. A qualifying verifier inspection appends an immutable fact
bound to the exact open cycle, reviewed content version and identity, verifier run/attestation, and
Verification Queue placement. `approve` and `reject` require that current fact; a later cycle or live
content/placement change cannot reuse it. Agents must inspect before deciding; an approval call is
not the discovery mechanism for lineage conflicts. Approval with `correction: none` signs that exact
inspected candidate and does not accept `file_text`; `correction: small` requires the complete
corrected candidate as `file_text`.

Marco-only continuations such as `supply-evidence`, `record-human-decision`, and `reopen` never
appear in an agent response's `allowed_actions`. When one is required, agent responses return an
empty action list and identify the exact private continuation in `data.required_admin_action`.
For an Evidence or Human Review hold (`required_admin_action: supply-evidence` or
`record-human-decision`), `read`, `inspect`, `reject`, and a `start` blocked by the existing held
operation all also return `data.submission_id` (the operation UUID the admin command itself
requires — not the task GID), `data.continuation_surface: private-admin`,
`data.connected_action_available: false`, an exact `data.admin_command` (including
`--resume-status` when the pending resume state is known from the preconstruction hold or the held
Verification cycle), and `data.after_resolution.legal_actions` naming what becomes legal once Marco
resolves the hold. `admin_command`/`connected_action_available` follow the same private-continuation
convention already used for `recover-lease` and permanent-run abandonment. These responses also return `data.directive`, a
ready-to-relay instruction telling the agent to hand the human the exact `admin_command`, wait for
confirmation it succeeded, and then resume the same submission (never start a new operation) with
the action named in `after_resolution.legal_actions`.

A historical task whose Material-change lines already fail `material-changes.format` or
`material-changes.field-count` is never rewritten automatically. Ordinary connected actions remain
blocked and return `workflow_recovery_required` with an empty `allowed_actions`, plus
`data.required_admin_action: manual-reconciliation`,
`data.continuation_surface: manual-reconciliation`,
`data.connected_action_available: false`, `data.admin_command: null`, the exact deduplicated
validation rules, and guidance that both the live evidence and its durable exact-content binding
must be reconciled under explicit Marco/admin authority.

Governed boundary responses include `data.validation_scope`, an ordered list drawn from:

- `structural-only` — deterministic parsing and schema/shape checks only;
- `transition-state` — current workflow state and legal-transition checks;
- `exact-content-identity` — exact live content or persisted identity binding;
- `agent-semantic-review` — the agent reported that it performed the semantic review;
- `provenance-signoff` — verifier provenance and exact signoff binding;
- `movement-confirmation` — destination movement confirmed by live reread.

The field reports the checks attempted at that boundary on both success and failure. It never means
Dish independently judged culinary truth, source quality, or substantive correctness. In
particular, Research preparation remains deterministic conformance rather than substantive
approval; Verification semantic review is supplied by the verifier; and successful `submit`
confirms state, identity, and movement without repeating semantic review.

Research `prepare` may normalize tool-owned process fields before writing: lifecycle status fields,
`Verification protocol release`, `Researched by`, `Verified by`, `Self-verified`, and, where the
current workflow requires it, `Material changes`. The response lists the fields actually changed in
`data.content_normalization.tool_owned_fields`. The returned `task.identity` and every later
exact-content check bind the complete live task *after* those disclosed normalizations; they do not
promise that the submitted candidate text was written byte-for-byte unchanged.

## Result codes and exit statuses

| Code | Exit | Meaning and handling |
|---|---:|---|
| `OK` | 0 | Deterministic command success. Continue the protocol’s semantic duty; a pass is not substantive approval. |
| `CONFIRMATION_REQUIRED` | 3 | No Planning operation or lease was opened. Preserve the returned challenge and make the documented fresh confirmed Planning call only after establishing the exact user-intent basis. |
| `INVALID_ARGUMENT` | 2 | Fix command syntax or required arguments; rerun only after correction. |
| `NOT_FOUND` | 2 | Confirm the task/operation identifier. Do not create substitute state. |
| `UNMANAGED_TASK` | 2 | Task is outside the governed Cooking scope; do not force it through this workflow. |
| `VALIDATION_FAILED` | 2 | Agent-correctable only when the protocol makes the defect agent-owned. Correct the exact task/candidate, update provenance or `Material changes` where required, reread, and rerun. |
| `WRONG_STATE` | 3 | Inspect the live task and operation; take only a returned legal action. |
| `AGENT_MISMATCH` | 3 | The caller is not the recorded actor. Use the correct actor or a protocol-valid ownership route. |
| `VERIFIER_FAMILY_MISMATCH` | 3 | Legacy compatibility code; treat as a closed transition and inspect. Current Verification independence is identity/attestation based, not opposite-family routing. |
| `PROTOCOL_INCOMPATIBLE` | 3 | The record belongs to an explicitly unsupported legacy workflow. Diagnostic reads remain available, but mutations are blocked; preserve the record and use the documented migration or manual disposition route. |
| `CONFLICT` | 3 | Stale identity, open-operation conflict, placement conflict, or another exact-state conflict. Preserve live content and restart/inspect as directed. |
| `HUMAN_ACTION_REQUIRED` | 3 | Stop normal agent workflow. This is valid only when the underlying protocol condition independently requires Marco; a tool message alone never creates Evidence or Human Review. |
| `BACKEND_REJECTED` | 4 | Backend proved non-application. Preserve state, diagnose, and rerun only when the reported cause is corrected. |
| `BACKEND_UNCERTAIN` | 5 | Outcome is ambiguous or only partially completed. Do not repeat the mutation. For operation-backed work, follow the durable `failed_step`, committed-effect fields, and `required_admin_action`; use Marco-only `dish-admin recover` with the reported outcome after a live reread. |
| `INTERNAL_ERROR` | 1 | Tooling failure. Preserve live task/content and report the command, identifiers, content identity, error, and diagnostics. |

The JSON `retryable` field is authoritative for mechanical retry advice. A correctable argument or candidate-validation failure is retryable when the operation remains open, the same Action remains legal, and no irreversible mutation occurred; corrected arguments may then be sent on that same operation. Identity, authorization, terminal-state, and exact-state conflicts remain non-retryable unless their specific contract says otherwise. Initialization failures always occur before request journaling, backend creation, or workflow execution. Ordinary availability failures remain `INTERNAL_ERROR / service_database_unavailable`; restore database availability, then repeat the exact call. Semantic-evidence failures retain `VALIDATION_FAILED / database_semantic_evidence_invalid`; repair the identified invariant records, then repeat the exact call. Both envelopes state `execution_occurred=false` and `request_id_consumed=false`, with a machine-readable `retry_condition`, so retry advice does not imply that an earlier mutation may have executed. The client envelope keeps only safe exception classification or semantic record diagnostics. Each semantic problem names the durable mutation provenance, exact failed source/target relationship and required predicate, and available record timestamps; the enclosing diagnostic also states when validation ran and whether the evidence came from committed database state or a connection-local transaction. The service error log retains the original exception and traceback plus selected request identifiers; command payload text, attestations, reasons, labels, and tokens are never serialized into either context. Even when `retryable` is true, correct the reported condition first. This field does not override request identity: exact response-loss replay reuses the original request UUID, while a fresh UUID represents new work. Never retry `BACKEND_UNCERTAIN` as a normal command.

For `reject`, route-specific validation is aggregate. A single `INVALID_ARGUMENT` response reports
all supplied fields incompatible with the selected route and includes that route's
`permitted_arguments`. Large corrections accept candidate/model fields and set
`pending-verification` automatically; Evidence and Human Review accept only their hold/resume
shape and do not accept candidate text or model. No rejection route, including Large, accepts an
independence-attestation field; every route inherits the exact attestation persisted at
Verification start.

## Interpreting outcomes

- **Tool pass:** deterministic conformance only. Continue the stage’s semantic work.
- **Agent-correctable finding:** fix the underlying protocol-owned defect, preserve required provenance, write/re-read through the tool, and rerun the same boundary check.
- **Possible Evidence or Human Review:** route there only when the underlying factual or judgment issue meets the protocol definition. Small/Large/Evidence/Human routing remains agent/protocol judgment.
- **Execution error or ambiguous result:** preserve task state and content. Report it as a tooling failure, not a dish blocker.
- **Tool/protocol disagreement:** fail closed, preserve the exact live task, stop the affected transition, and report the conformance defect. The protocol wins.

## Material classification

`material_classification` applies only to the canonical body diff of a post-signoff change from its signed baseline. It is required when that body changed and rejected when no body diff exists. The caller proposes `material` or `non-material`; Dish may force the effective classification to material when a protocol-defined material path changed. The result reports the classified subject, requested and effective values, forced reasons, and route under `data.material_classification`; the exact override reasons are always present as `data.material_classification.forced_material_reasons` (an empty array when Dish did not force material). Effective material changes enter Verification; accepted non-material changes preserve the exact prior signoff. That lineage remains valid across successive accepted non-material check-ins: each completed check-in binds its confirmed identity to the same originating approved cycle rather than requiring or inventing a new approval. When the canonical classifier proves a quantity or portion change, Dish records the generated Material-change entry as `Large` even if the caller started the change as `small`; other material edits retain their applicable Small/Large rule.

## Material-change audit lifecycle

Material changes use the documented seven-field order: date, agent, self-reported model metadata,
concrete change, reason, Small/Large materiality, and verification state. `model` is caller-supplied
display metadata, not authenticated runtime provenance; new lines render it as `self-reported model:
<value>`. Dish NFC-normalizes audit-bearing caller text and rejects controls, format characters,
surrogates, line/paragraph separators, and grammar delimiters before an operation or backend write.
Existing unlabeled provenance remains parseable. Dish owns existing canonical history after the
first baseline: later candidates may preserve it exactly or omit it for normalization, but cannot
rewrite it. The independent approval transition finalizes every pending entry in the reviewed
correction chain, and `submit` fails closed while any entry remains `pending-verification`.

## Pre-construction Research hold

During a fresh initial Research operation, `reject --route evidence|human-review --resume-status pending-research` may hold the operation before `prepare`. The durable record says `Research blocked before construction`, retains the originating task, agent/run, request UUID, route, reason, resolver, timestamp, and records `candidate_content_existed: false`. It must not create a candidate identity, Verification cycle, or Material-change record. `supply-evidence` or `record-human-decision` resolves the same operation back to `prepare_required`; other resume states and candidate-bearing resolutions fail closed. A new `start` blocked by any held operation returns the existing submission ID, held phase, exact required admin action, and `Marco/admin <action>` resolver guidance; it never creates another operation.

## Rerun rules

- Reread or inspect before deciding what to rerun.
- A stale baseline requires a new exact operation; never overwrite the live edit.
- A confirmed content write is naturally idempotent and must not be repeated.
- A confirmed content write, Verification signoff, and destination submission movement are independent completion facts. Recovery reconciles only interrupted backend attempts; it does not invent signoff or treat a Research/Verification handoff as destination submission.
- A successful `approve` returns `submit`; the verifier runs it in the same pass.
- If the task is already at its valid destination, `submit` records a confirmed no-op `destination_submission` movement attempt and then completes idempotently.
- Approval never implies final movement. Planning and Verification handoffs use their own movement purposes; only a confirmed `destination_submission` attempt satisfies final submission movement.
- A final destination failure leaves approved content intact in recoverable `ready_move_failed` state. When live evidence proves the move was not applied, unchanged `submit` is the only legal retry. When the destination is unresolved or illegal, the agent receives no mutation action and `data.required_admin_action: repair-destination`; Marco runs `dish-admin repair-destination OPERATION_ID --destination-section-gid GID --reason TEXT`. The command validates the live section, changes only `Planning brief / Destination section`, preserves the immutable approved identity, records a linked repaired identity, and returns `submit`. The later `submit` uses that repaired identity and must not repeat the content write or approval.

## Human admin response contract

`dish-admin` defaults to human-readable output when stdout is an interactive terminal.  Use
`--json` for the canonical service envelope and `--verbose` to include rule IDs and the raw
envelope beneath the human explanation.  Output flags may appear before or after the subcommand.

Start blocked-task diagnosis with:

```sh
dish-admin inspect TASK_GID_OR_OPERATION_ID
```

The result states what the task is waiting for, who owns it, and the safe action Marco can take.
It may return a command template only when Marco must supply decision or reason text.  Agents relay
the returned `human_action` and its rendered command exactly; they do not reconstruct flags from
cycle, hold, lease, or identity fields.

For a global read-only inventory, run:

```sh
dish-admin attention
```

The scan checks active operations, non-completed abandonments, unreleased actor leases, and
unresolved operation executions. It classifies only persisted/current evidence as safe multi-step
recovery, needing Marco, unsafe/uncertain, or healthy. It never expires, abandons, reconciles, or
changes workflow state. Ambiguous multiple-attempt authority is reported as needing Marco; the scan
does not choose a lease ID. One item failing inspection does not suppress unrelated items and is
reported as unsafe with its exact operation ID.

A recovery-bearing result exposes one structured `human_action` with: command and fixed bindings,
required human input, summary, plain-language `details`, exact effect, structured `context`, and
after-success instruction. Agents must relay the details before the command rather than making Marco
infer approval scope or consequences from shell arguments. Compatibility fields
`admin_command`, `admin_command_is_template`, and `admin_command_template` describe the same action.
Generated commands must parse on the current `dish-admin` CLI.  When exact recovery cannot be
chosen safely, the response directs Marco to `dish-admin inspect` rather than listing internal IDs
for manual selection.

For leases:

- `recover-lease` is valid only when the same durable agent run will continue;
- another run never receives `recover-lease` as ownership transfer;
- a different run may receive exact `safe-reclaim` only when the clean mechanical predicate passes;
- an open Verification cycle bound to an unavailable run exposes no `approve` or `reject`;
- `inspect` exposes safe reclaim when mechanically proven, deterministic recovery first when execution/effect evidence is unresolved, and formal abandonment only when that remains the correct recovery route.

An agent attempting `reject --route human-review` must either supply the Human Review preflight
fields or receive a non-mutating `CONFIRMATION_REQUIRED` response that asks for the evidence, repairs
considered, and the specific unresolved Marco-only choice. The preflight explicitly treats a
reasonable defensible estimate with stated assumptions as valid when exact yield/portion facts are
unknowable. Nutrition uses the served edible portion expected to be consumed, excluding bones,
shells, discarded cooking liquid, drained/rendered fat, and other material not eaten. A gross-raw or
otherwise poorly supported estimate cannot establish a hard-limit breach. Uncertainty alone is not a
blocker, but Dish must not invent a midpoint or other false precision when no single estimate is
defensible. The durable structured numeric blocker represents
one estimate, its limit, and its excess/shortfall; it does not represent a range. If the exact governed repair can already be constructed,
the agent should use a Large correction so Dish queues that exact semantic proposal instead of
creating an open-ended Human Review hold. Retrying a genuine escalation uses a fresh request ID.

Before a Large correction queues governed approval, Dish checks for small governed-text edits that
may be incidental cleanup. Such an edit is not automatically declared non-semantic. Instead, Dish
requires the agent either to restore the governed text exactly to the live baseline or explicitly
name the intended governed field on the retry. This is a controlled no-effect
`CONFIRMATION_REQUIRED` result, not `BACKEND_UNCERTAIN`; the corrected retry uses a fresh request ID.
The ordinary exact governed-approval protections still apply to any intentionally changed governed
field.

When a Large correction needs governed authority, Dish validates the candidate as one semantic
proposal. The response includes the full rationale and linked change set: why the candidate fails,
the concrete cause, why ordinary correction is not preferred, why the proposal follows Marco's
settled intent, alternatives considered, and every title, Planning, Decision, or Research
contradiction resolved by that same candidate. Dish rejects mechanically coherent but semantically
inconsistent proposals, including non-main role plus main-meal nutrition exemptions or a non-main
title marker that disagrees with `Role`.

If authority is missing, Dish stores the exact candidate and all governed before/after values once,
returns `semantic_proposal_queued`, releases proposer lease ownership, and marks the task safely
parked. The proposing agent may continue unrelated batch work when `batch_may_continue=true`; it must
not keep mutating the parked operation. The same flag is returned after a durable Human Review,
Evidence, or completed Large-correction handoff. In an explicit batch, the agent tracks handled task
GIDs for that run and skips them if section pagination returns them again.

`dish-admin review-queue` aggregates pending semantic proposals and Verification Human Review holds.
Each item carries a compact `review_summary`: outcome, material issue, quantified blocker when one was
recorded, the decision where applicable, and the simplest next step. `review-inspect` accepts either
the durable UUID or the current queue number. Semantic bundles use `review-approve`/`review-reject`.
For an unanswered Verification Human Review item the normal operator surface is also the review flow:
`review-inspect` presents `review-approve REVIEW_ID --reason '<Marco decision>'` or
`review-reject REVIEW_ID --reason '<why the escalation is invalid>'`; low-level hold IDs and
`record-human-decision` remain internal/compatibility mechanics rather than the normal UX. Formal
Human Review is reserved for consequential governed authorization. Ordinary clarification/preference
may be used directly, while an intentional choice worth preserving may be appended as an attributed
`Human — Marco:` Decision without formal admin authorization; such an append does not itself authorize
another governed field mutation, and rewriting/removing an existing Decision remains governed. Substantive
approval persists Marco's decision and follows the hold's stored resume route: `pending-verification`
opens a new Verification cycle, while `pending-research` returns the task to Research and completes
the held Verification operation. When the candidate is unchanged, the same still-live verifier run
that was already independent of the constructor/material editor may resume the interrupted pass on
the new cycle. A material edit makes its resolver a material editor and still requires fresh
independent signoff. Dismissal is intentionally different: it always releases the
unchanged candidate to fresh Verification regardless of stored resume status, preserves the original
issue and dismissal reason in audit/context, and fabricates no Marco decision. The outer
machine-readable command remains the public wrapper Marco invoked (`review-approve` or
`review-reject`); lower-level compatibility command names remain internal audit/state semantics.
Neither path silently edits or authorizes governed fields. For semantic proposals, normal
`review-inspect` shows every linked candidate change covered by approval/application before the
approve command; `--verbose` adds rationale, evidence/provenance, protocol mechanics, IDs and
diagnostics. Approval is atomic across that exact displayed bundle and does not by itself apply, sign, or
submit the task. `review-approve` first durably persists that approval and then invokes a separate
mechanical application action in Dish. The application rereads/revalidates the approved immutable
bundle, and only that second durable action may change canonical content. If mechanical application
fails, approval remains persisted and the failure reports that fact so retry/recovery can continue
without asking Marco to approve the same bundle again.

Approved proposals are detached from the proposing run. The normal path does not require another AI
agent: Dish claims the bundle under its mechanical application actor, verifies the original baseline,
installs only the immutable stored candidate, consumes linked authorizations, closes the interrupted
cycle as Large, and opens a fresh Verification cycle for independent signoff. `dish proposals
--agent AGENT` and `dish apply-proposal PROPOSAL_ID --agent AGENT --model MODEL` remain low-level
recovery/testing surfaces for an approved bundle whose normal application did not complete. A
low-level applying invocation does not inherit the proposer's run identity or Verification
independence. A rejected proposal creates no authorizations or task edit;
Dish closes the proposing cycle against the unchanged baseline and opens a fresh Verification cycle
for a different proposal. The same semantic change bundle cannot be requeued unchanged after Marco
rejects it. An unused standalone operation-bound authorization still inherits across an exact
abandonment successor.

## Troubleshooting checklist

1. Save the complete JSON result and process exit status.
2. Cross-check `journalctl -u dish-service.service` for the corresponding request.
3. Run `dish read TASK_GID --agent AGENT` and, when an operation exists, `dish inspect OPERATION_ID --agent AGENT`.
4. Compare the reported live identity, reviewed/signed identity, placement, schema version, and legal actions.
5. For compatibility failure, confirm `DISH_HONEST_PATH`, `DISH_VERSION`, schema assets, and the exact supported protocol/schema pair.
6. For migration required, stop normal commands and ask Marco to run `dish-admin migrate`.
7. For a `started` or `uncertain` write/movement, do not retry the backend mutation. Use `dish-admin recover` after a live reread; recovery must match persisted expected/intended evidence and records the reconciliation outcome durably. This includes an interrupted destination-repair content write.
8. For an unrecoverable destination failure, use only the returned `repair-destination` admin action; do not reopen Verification or edit the task directly.
9. For tool/protocol disagreement, preserve the task unchanged and report both the protocol clause and tool rule.

## Verification hold continuation

A third non-approved Verification round ending in a Large correction returns no agent action and advertises
`dish-admin resolved <operation-id>`. Resolution preserves the held recipe candidate, changes only
the workflow state back to `pending-verification`, creates the next independent Verification cycle,
and does not approve or sign off the task. `dish-admin reopen` remains available only for an actual
substantive reset with its existing evidence contract.


## Hold observability and resolution binding

`dish-admin holds` is the read-only Marco/admin inventory for every open Evidence or Human Review hold. It classifies pre-construction Research, ordinary Verification Evidence/Human Review, and automatic two-pass Verification holds separately, reports the exact required admin action, task title/GID/link, question, operation and cycle identifiers, and the persisted hold identity. Durable resolution commands must include the displayed task GID and, for Verification holds, the displayed cycle ID and hold identity; Dish rejects stale or mismatched commands before mutation. Quantified-limit blockers are recorded at `reject` time as a complete metric/actual/limit/delta/unit/basis set in the existing operation-step and audit JSON.

## Deterministic validation recovery metadata

Agent-correctable deterministic document findings include the submitted value or local context plus
optional `expected`, `example`, `recovery`, and `related` fields. Existing `rule` identifiers remain
stable; human-facing messages use document-facing terms and locations. For example,
`document.recognition-empty` identifies canonical line 2 as the dish-summary/meal-role sentence and
returns the submitted first two header lines, the accepted shape, and the exact insertion point.

When a `VALIDATION_FAILED` result contains only correctable document findings and the same command is
still present in authoritative `allowed_actions`, `data.retry` describes the safe continuation:
`mode=correct_then_retry`, the command to retry, whether the same operation and Verification cycle
remain usable, whether a fresh request ID is required, and whether any mutation occurred. This is
not permission to repeat the unchanged request. Correct the candidate first and follow the returned
retry metadata. The retry block is omitted when live policy no longer permits the command or the
failure is not an agent-owned document correction.
