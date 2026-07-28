# Dish runtime contract reference

Command syntax and invocation live in `dish --help` / `dish <stage> --help` / `dish-admin --help`,
setup lives in `dish/README.md`, and internal design lives in [`architecture.md`](architecture.md).
This document is the reference for what a response actually means once you've made a call: the JSON
envelope shape, exit-status handling, and recovery.

## Authority and scope

The live Asana Cooking task is authoritative for title, body, workflow state, provenance, and cooking instructions. Agents access protocol-managed Cooking tasks only through `dish`; they do not read or write those tasks through the generic Asana CLI. Planning's read-only lookup of completed cooking history through the generic `asana` CLI is the one deliberate exception. It does not authorize writes to governed tasks.

The `ai-tools` checkout supplies deterministic validation and the client executables. In live multi-agent mode, one laptop-hosted `dish-service` process is the sole writable authority for operation state, leases, Asana credentials, audit/recovery, backup, and all governed task mutations. A repository copy or copied SQLite database is never a cross-agent lock.
The single-agent local test path remains available only for controlled development and is not live multi-agent authority. It requires explicit `DISH_MODE=local` and a separate database that has never been marked as service-owned.

Candidate files are ephemeral complete-text inputs. In service mode the client reads the file and sends its text; the server never opens a client filesystem path. The live task is reread before mutation and after every write or move. Do not edit a candidate after recording the identity supplied to Verification.

## Access-path contract

| Caller | Network path | Credential | Permitted surface |
|---|---|---|---|
| `dish` CLI | private Tailscale Serve/tailnet endpoint | agent CLI bearer token | bounded agent commands and lease renewal |
| `dish-admin` | private Tailscale Serve/tailnet endpoint | separate Marco-admin bearer token | admin workflow, stale-lease recovery, backup/restore |
| GPT Action | public Tailscale Funnel endpoint on its own HTTPS port | dedicated Action bearer token | `/v1/action/*` commands and Action lease renewal only |
| local tests | direct local application mode | local Asana test credential when required | controlled single-agent development only |

Live client environments set all of:

```text
DISH_LIVE_MODE=1
DISH_MODE=service
DISH_SERVICE_URL=<private service URL>
DISH_CLIENT_RUN_ID=<canonical lowercase UUID for this run>
```

The CLI adds `DISH_SERVICE_TOKEN`; Marco's admin shell adds `DISH_ADMIN_TOKEN`. The GPT Action stores only `DISH_SERVICE_ACTION_TOKEN` in its Action authentication configuration. No client receives the service database path or Asana credential.

The service host is the only place that defines `ASANA_PAT` or `ASANA_ENV`. It runs one process, enforced by a host file lock tied to the shared database. The process exposes two loopback listeners:

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

Agent-facing action guidance is authoritative even on failures. When an operation-scoped command is
rejected, `allowed_actions` reports the currently legal exposed continuation when one exists. A
retryable candidate-validation failure therefore keeps the same corrective command available.
Fresh bare tasks created by `create` report `data.required_start_kind: planning`. A completed
cross-stage handoff reports `start` and the required start kind even though the old operation itself is
terminal. Verification `start` includes `inspect` so the agent can review exact identity, provenance,
and lineage before deciding. Task-level `read` responses expose any active operation, its submission
ID, workflow state, and principal-filtered next actions. Successful operation-scoped lease renewal
includes both `task_gid` and `submission_id`; renewal of a terminal operation reports
`WRONG_STATE / operation_not_open` with the terminal status.

## Service ownership and leases

The durable `operations` constraint is the one-active-operation-per-task lock. A separate durable `operation_execution_claims` row serializes mutation execution for that operation, and partial unique indexes prohibit multiple unresolved writes or movements for it. The service also writes a persistent ownership sidecar and process lock derived from the canonical database target rather than the caller-supplied pathname; direct local CLI/admin mode will not open the same database through a symlink alias, including while the service process is stopped. `service_leases` bind the current actor to an owner identity and run identity with a renewable expiry. Workflow handoff may release the actor lease, but it does not release the task operation lock. `allowed_actions` is principal-aware: a different or expired run receives no ordinary mutation actions even when the underlying workflow phase has one. Read-only inspection never mutates lease state. Expired leases fail closed and require Marco to run `dish-admin recover-lease`; recovery releases stale ownership but does not transfer the workflow to Marco. Only a run whose durable actor lineage matches the required workflow role may reclaim a missing lease. Admin hold/recovery continuations use temporary request-scoped leases and return the operation unleased for the next valid actor.

A terminal lease is released only after the operation is terminal and every declared step and
ambiguous write/movement attempt has a durable completion outcome. Lease acquire, renew, release,
and terminal checks are transactional. Configuration requires the lease TTL to exceed one
maximum-duration Asana SDK call plus the recovery safety margin; the default TTL is substantially
longer. This validation is not a whole-command deadline: one Dish command may perform several
sequential Asana calls. If post-success lease or owned-resource cleanup fails after the governed
mutation committed, the original command still returns success. A safe fallback may release the
lease and report `service_cleanup_warning`; otherwise `service_recovery_required` suppresses
follow-on actions and explicitly tells the client not to retry the mutation. Ordinary full-state
write and approval retries remain naturally idempotent by exact live-state comparison. In service
mode, all agent mutations are replay-bound, and every externally callable agent, administrative,
lease, and backup mutation requires a client-generated UUID `client.request_id`; reads (`sections`,
`read`, `inspect`, and `health`) do not. This includes `create`, `start`, `prepare`, `approve`,
`reject`, and `submit`. Reuse that UUID only when retrying the exact same logical call after a lost
response.

## Request replay contract

In service mode, the first authoritative outcome of every agent, administrative, lease,
backup-create, or backup-restore mutation is durably bound to `client.request_id`. Request identity
includes the command, canonical arguments, authenticated owner identity, and run identity.

- Missing or malformed request IDs are rejected before a request record exists. Once the UUID is accepted, expected argument, state, authorization, and workflow failures are stored just like successes.
- A repeated completed request returns the original stored result with `data.request_replayed: true` and `data.request_id`.
- Reusing an ID for a different command, owner/run, or arguments returns non-retryable `CONFLICT` with `service_request_identity_conflict`.
- A matching pending or uncertain request is never blindly executed again. `start` may be reconciled only when exact durable operation and live-state evidence proves the original result. For multi-step workflow mutations routed through the current operation service, an active execution claim remains pending; a dead execution is reconstructed from its request-scoped durable baseline and exact changed attempts, content versions, Verification cycles, workflow steps, actor facts, and operation state.
- Reconstructed partial-effect failures return non-retryable `BACKEND_UNCERTAIN` with `write_committed`, `move_committed`, `cycle_created`, `failed_step`, `authoritative_task_identity`, `required_admin_action: recover`, `required_admin_outcome`, and `safe_to_retry: false`. These values are evidence-backed and stable across restart. Confirmed writes or movements are never repeated by recovery.
- A completed `submit` is replayed from the request ledger. A fresh request ID for the same already-completed logical submission is also satisfied from exact signed-content and destination-movement evidence, without reacquiring a lease or repeating the external movement.
- Ordinary request records live in `service_requests` and survive service restart. `backup-restore` uses an atomic sibling sidecar journal because the restore replaces the database that contains ordinary request records. The sidecar binds the request to the selected backup and terminal result across replacement and restart.

The bundled CLI and admin clients generate an ID internally for each first mutation call, but their
command-line interfaces neither accept a request ID nor expose the generated value after a
transport failure. A CLI caller therefore cannot perform an exact request replay after response
loss and must inspect live and durable state instead of blindly rerunning the mutation. A
programmatic HTTP client may supply and retain an explicit request ID. GPT Action calls must supply
it explicitly through the imported schema. The public schema marks both `client.request_id` and
`client.run_id` as canonical lowercase UUIDs; `run_id` remains stable for the whole agent run.

## Health, backup, and startup

`GET /health` exists only on the private listener. It checks:

- current SQLite schema and semantic evidence validation;
- exact Honest protocol/task-schema compatibility;
- Asana access and required Cooking section registry;
- pending invocation-audit repairs;
- active operations and active/expired leases.

At startup the service validates the database, including semantic impossibilities such as duplicate unresolved attempts or active leases on terminal operations, resolves Honest compatibility, and replays pending invocation-audit repairs. Repair workers claim each repair transactionally so concurrent workers cannot emit duplicate events. Durable service-request records and any restore-fault marker survive process restart. Listener readiness depends on valid service configuration, not healthy external or recoverable data dependencies. An Asana outage, an invalid live database, compatibility failure, or a restore-fault marker therefore leaves the private service available for health and administrative diagnosis or restore; health remains unhealthy and workflow mutations fail before entering application mutation code. Backup creation and lease renewal remain available only when their own database prerequisites are healthy.

`dish-admin backup-create` produces a managed SQLite snapshot using the online backup API, including committed WAL state, and validates the complete current database contract. Legacy pre-v3 quarantine backups use the same online-backup rule and replace a previously invalid legacy artifact rather than treating its filename as proof. `dish-admin backup-restore` accepts only a managed backup identifier, copies it into a temporary candidate, migrates the candidate to the current schema, validates it, and replaces the database atomically without modifying the source backup. It attempts a validated pre-restore snapshot when the live database is readable, but a corrupt or semantically invalid live database does not prevent restore. Restore is exclusive against ordinary requests; ordinary requests remain concurrent outside restore. Managed backup identifiers cannot resolve through symlinks outside the backup directory. Restore durably pre-arms its fault marker before database replacement; if the marker cannot be persisted, replacement does not start. A failed restore reports whether rollback was actually proven. If automatic rollback cannot be proven, the pre-armed marker remains beside the database; health remains unhealthy and workflow mutations stay disabled across restart until manual recovery or a successful validated restore clears it. Reported restored hashes and sizes describe the bytes actually installed after migration.

Admin recovery remains specific rather than generic:

- `recover-lease` releases only an expired actor lease; it never assigns workflow ownership to the admin caller;
- `recover` reconciles ambiguous backend evidence by live reread;
- `repair-destination` changes only the canonical Planning destination after an unrecoverable final movement failure, preserving the original approval and creating linked repair evidence for a later movement-only `submit`;
- `discard` cancels only a provably unapplied operation;
- `supply-evidence`, `record-human-decision`, and `reopen` retain their existing protocol meanings.

There is intentionally no general-purpose `unblock` mutation.

## Transport and external scope boundary

HTTP bodies are executed only when exactly the declared `Content-Length` bytes were received; a short body is rejected before JSON parsing. Tokens reject surrounding whitespace, numeric timeouts must be finite and positive, and private lease/backup routes reject undeclared fields just like Action/admin routes. The client closes failed HTTP response objects and maps abrupt disconnects into structured service-unavailable envelopes.

Every complete task reread reasserts Cooking-project membership, so a task removed between the initial scope check and the authoritative read cannot open or continue an operation with a null placement. Asana section enumeration follows all pages. Verification approval/rejection must repeat the exact verifier run and independence attestation recorded at Verification start; actor facts are scoped to an operation, allowing a run to participate legally in a later operation without rewriting earlier lineage.

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
- `errors` contains structured findings with a `rule` and any supporting fields.

Internal workflow permission `verify` is exposed as Action/CLI command `start`. When `start` is the
required handoff command, `data.required_start_kind` identifies its required `kind`: `initial` after
Planning and `verification` after Research. Clients must not look for a separate `verify` Action.

Verification `start` and `inspect` expose `data.verification_lineage`. `candidate_runs` lists the
constructor and material-editor run facts that contribute to verifier independence enforcement.
`current_run` reports the authenticated caller run, whether it is eligible to verify, and the exact
disqualifying role/rule when it is not. Agents must inspect this before deciding; an approval call is
not the discovery mechanism for lineage conflicts.

Marco-only continuations such as `supply-evidence`, `record-human-decision`, and `reopen` never
appear in an agent response's `allowed_actions`. When one is required, agent responses return an
empty action list and identify the exact private continuation in `data.required_admin_action`.

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

The JSON `retryable` field is authoritative for mechanical retry advice. Even when true, correct the reported condition first. Never retry `BACKEND_UNCERTAIN` as a normal command.

For `reject`, route-specific validation is aggregate. A single `INVALID_ARGUMENT` response reports
all supplied fields incompatible with the selected route and includes that route's
`permitted_arguments`. Large corrections accept candidate/model fields and set
`pending-verification` automatically; Evidence and Human Review accept only their hold/resume
shape and do not accept candidate text, model, or independence-attestation fields.

## Interpreting outcomes

- **Tool pass:** deterministic conformance only. Continue the stage’s semantic work.
- **Agent-correctable finding:** fix the underlying protocol-owned defect, preserve required provenance, write/re-read through the tool, and rerun the same boundary check.
- **Possible Evidence or Human Review:** route there only when the underlying factual or judgment issue meets the protocol definition. Small/Large/Evidence/Human routing remains agent/protocol judgment.
- **Execution error or ambiguous result:** preserve task state and content. Report it as a tooling failure, not a dish blocker.
- **Tool/protocol disagreement:** fail closed, preserve the exact live task, stop the affected transition, and report the conformance defect. The protocol wins.

## Material classification

`material_classification` applies only to the canonical body diff of a post-signoff change from its signed baseline. It is required when that body changed and rejected when no body diff exists. The caller proposes `material` or `non-material`; Dish may force the effective classification to material when a protocol-defined material path changed. The result reports the classified subject, requested and effective values, forced reasons, and route. Effective material changes enter Verification; accepted non-material changes preserve the exact prior signoff.

## Material-change audit lifecycle

Material changes use the documented seven-field order: date, agent, self-reported model metadata,
concrete change, reason, Small/Large materiality, and verification state. `model` is caller-supplied
display metadata, not authenticated runtime provenance; new lines render it as `self-reported model:
<value>`. Dish NFC-normalizes audit-bearing caller text and rejects controls, format characters,
surrogates, line/paragraph separators, and grammar delimiters before an operation or backend write.
Existing unlabeled provenance remains parseable. Dish owns existing canonical history after the
first baseline: later candidates may preserve it exactly or omit it for normalization, but cannot
rewrite it. The independent approval transition finalizes the latest pending entry, and `submit`
fails closed while the latest relevant entry still claims `pending-verification`.

## Pre-construction Research hold

During a fresh initial Research operation, `reject --route evidence|human-review --resume-status pending-research` may hold the operation before `prepare`. The durable record says `Research blocked before construction`, retains the originating task, agent/run, request UUID, route, reason, resolver, timestamp, and records `candidate_content_existed: false`. It must not create a candidate identity, Verification cycle, or Material-change record. `supply-evidence` or `record-human-decision` resolves the same operation back to `prepare_required`; other resume states and candidate-bearing resolutions fail closed.

## Rerun rules

- Reread or inspect before deciding what to rerun.
- A stale baseline requires a new exact operation; never overwrite the live edit.
- A confirmed content write is naturally idempotent and must not be repeated.
- A confirmed content write, Verification signoff, and destination submission movement are independent completion facts. Recovery reconciles only interrupted backend attempts; it does not invent signoff or treat a Research/Verification handoff as destination submission.
- A successful `approve` returns `submit`; the verifier runs it in the same pass.
- If the task is already at its valid destination, `submit` records a confirmed no-op `destination_submission` movement attempt and then completes idempotently.
- Approval never implies final movement. Planning and Verification handoffs use their own movement purposes; only a confirmed `destination_submission` attempt satisfies final submission movement.
- A final destination failure leaves approved content intact in recoverable `ready_move_failed` state. When live evidence proves the move was not applied, unchanged `submit` is the only legal retry. When the destination is unresolved or illegal, the agent receives no mutation action and `data.required_admin_action: repair-destination`; Marco runs `dish-admin repair-destination OPERATION_ID --destination-section-gid GID --reason TEXT`. The command validates the live section, changes only `Planning brief / Destination section`, preserves the immutable approved identity, records a linked repaired identity, and returns `submit`. The later `submit` uses that repaired identity and must not repeat the content write or approval.

## Troubleshooting checklist

1. Save the complete JSON result and process exit status.
2. Run `dish read TASK_GID --agent AGENT` and, when an operation exists, `dish inspect OPERATION_ID --agent AGENT`.
3. Compare the reported live identity, reviewed/signed identity, placement, schema version, and legal actions.
4. For compatibility failure, confirm `DISH_HONEST_PATH`, `DISH_VERSION`, schema assets, and the exact supported protocol/schema pair.
5. For migration required, stop normal commands and ask Marco to run `dish-admin migrate`.
6. For a `started` or `uncertain` write/movement, do not retry the backend mutation. Use `dish-admin recover` after a live reread; recovery must match persisted expected/intended evidence and records the reconciliation outcome durably. This includes an interrupted destination-repair content write.
7. For an unrecoverable destination failure, use only the returned `repair-destination` admin action; do not reopen Verification or edit the task directly.
8. For tool/protocol disagreement, preserve the task unchanged and report both the protocol clause and tool rule.

The corpus migration rehearsal and live cutover remain separately authorized Step 12 work. Passing this Step 11 contract does not itself authorize production Cooking-task activation.
