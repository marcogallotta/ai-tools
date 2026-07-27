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
DISH_CLIENT_RUN_ID=<unique run identity>
```

The CLI adds `DISH_SERVICE_TOKEN`; Marco's admin shell adds `DISH_ADMIN_TOKEN`. The GPT Action stores only `DISH_SERVICE_ACTION_TOKEN` in its Action authentication configuration. No client receives the service database path or Asana credential.

The service host is the only place that defines `ASANA_PAT` or `ASANA_ENV`. It runs one process, enforced by a host file lock tied to the shared database. The process exposes two loopback listeners:

- private CLI/admin listener, intended for Tailscale Serve;
- Action-only listener, intended for Tailscale Funnel.

The public listener does not route private CLI, admin, health, migration, recovery, or backup endpoints. HTTP status remains transport information; workflow meaning remains in the canonical JSON result code. On the GPT Action surface, expected authenticated Dish rule outcomes (including `INVALID_ARGUMENT`, state conflicts, and validation failures) use HTTP 200 so the Action runtime returns the canonical envelope to the agent instead of reclassifying it as a transport failure. Authentication and authorization failures retain HTTP 401/403, and unexpected server failures retain HTTP 500.

## Service ownership and leases

The durable `operations` constraint is the one-active-operation-per-task lock. The service also writes a persistent ownership sidecar for its database; direct local CLI/admin mode will not open a service-owned database, including while the service process is stopped. `service_leases` bind the current actor to an owner identity and run identity with a renewable expiry. Workflow handoff may release the actor lease, but it does not release the task operation lock. `allowed_actions` is principal-aware: a different or expired run receives no ordinary mutation actions even when the underlying workflow phase has one. Read-only inspection never mutates lease state. Expired leases fail closed and require Marco to run `dish-admin recover-lease`; recovery releases stale ownership but does not transfer the workflow to Marco. Only a run whose durable actor lineage matches the required workflow role may reclaim a missing lease. Admin hold/recovery continuations use temporary request-scoped leases and return the operation unleased for the next valid actor.

A terminal lease is released only after the operation is terminal and every declared step and ambiguous write/movement attempt has a durable completion outcome. If post-success lease finalization fails after the governed mutation committed, the original command still returns success with `service_recovery_required`, suppresses follow-on actions, and explicitly tells the client not to retry the mutation. Ordinary full-state write and approval retries remain naturally idempotent by exact live-state comparison. The response-loss-sensitive `create` and non-verification `start` commands additionally require a client-generated UUID `client.request_id`. Reuse that same request ID only when retrying the exact same logical call after a lost response; never reuse it for different arguments. A completed request returns its stored result, a provably committed `start` is reconciled to the existing operation, and an unresolved `create` fails `BACKEND_UNCERTAIN` rather than risking a duplicate task.

## Request replay contract

`create` and non-verification `start` cross a response-loss boundary where blindly repeating the HTTP
call could create a duplicate task or hide an already-created operation. Their private and Action
requests therefore include `client.request_id`, a UUID chosen before the first attempt.

- Reuse the same request ID only for the same command, authenticated owner/run, and arguments.
- A repeated completed request returns the original stored result with
  `data.request_replayed: true`.
- Reusing an ID for different work returns `CONFLICT`.
- A process interruption after `start` is reconciled only when exact durable operation evidence proves
  the existing operation.
- A process interruption around `create` cannot be inferred safely from local state, so the same request
  returns `BACKEND_UNCERTAIN` and must not be repeated under a new request ID until Marco has inspected
  live Asana state.

The bundled HTTP clients generate an ID for a first call, but a caller handling transport failure must
retain and reuse that value. GPT Action calls must supply it explicitly through the imported schema.
Other commands keep their existing exact-state and durable-attempt retry rules.

## Health, backup, and startup

`GET /health` exists only on the private listener. It checks:

- current SQLite schema and semantic evidence validation;
- exact Honest protocol/task-schema compatibility;
- Asana access and required Cooking section registry;
- pending invocation-audit repairs;
- active operations and active/expired leases.

At startup the service validates the database, resolves Honest compatibility, and replays pending invocation-audit repairs. Durable service-request records and any restore-fault marker survive process restart. An Asana outage may leave the process available for health, backup, lease renewal, and diagnosis, but all workflow mutations fail before entering application mutation code.

`dish-admin backup-create` produces a managed SQLite snapshot using the online backup API and validates the complete current database contract. `dish-admin backup-restore` accepts only a managed backup identifier, creates a pre-restore snapshot, validates the restore candidate, replaces the database atomically, and rolls back if validation fails. Restore is serialized against every request. A failed restore reports whether rollback was actually proven. If automatic rollback cannot be proven, the service writes a fault marker beside the database; health remains unhealthy and workflow mutations stay disabled across restart until manual recovery or a successful validated restore clears the marker.

Admin recovery remains specific rather than generic:

- `recover-lease` releases only an expired actor lease; it never assigns workflow ownership to the admin caller;
- `recover` reconciles ambiguous backend evidence by live reread;
- `discard` cancels only a provably unapplied operation;
- `supply-evidence`, `record-human-decision`, and `reopen` retain their existing protocol meanings.

There is intentionally no general-purpose `unblock` mutation.

## JSON response contract

Every invocation writes exactly one compact JSON object to stdout:

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
| `BACKEND_UNCERTAIN` | 5 | Outcome is ambiguous. Do not repeat the mutation. Preserve the task and use Marco-only recovery after a live reread. |
| `INTERNAL_ERROR` | 1 | Tooling failure. Preserve live task/content and report the command, identifiers, content identity, error, and diagnostics. |

The JSON `retryable` field is authoritative for mechanical retry advice. Even when true, correct the reported condition first. Never retry `BACKEND_UNCERTAIN` as a normal command.

## Interpreting outcomes

- **Tool pass:** deterministic conformance only. Continue the stage’s semantic work.
- **Agent-correctable finding:** fix the underlying protocol-owned defect, preserve required provenance, write/re-read through the tool, and rerun the same boundary check.
- **Possible Evidence or Human Review:** route there only when the underlying factual or judgment issue meets the protocol definition. Small/Large/Evidence/Human routing remains agent/protocol judgment.
- **Execution error or ambiguous result:** preserve task state and content. Report it as a tooling failure, not a dish blocker.
- **Tool/protocol disagreement:** fail closed, preserve the exact live task, stop the affected transition, and report the conformance defect. The protocol wins.

## Rerun rules

- Reread or inspect before deciding what to rerun.
- A stale baseline requires a new exact operation; never overwrite the live edit.
- A confirmed content write is naturally idempotent and must not be repeated.
- A confirmed content write, Verification signoff, and destination submission movement are independent completion facts. Recovery reconciles only interrupted backend attempts; it does not invent signoff or treat a Research/Verification handoff as destination submission.
- A successful `approve` returns `submit`; the verifier runs it in the same pass.
- If the task is already at its valid destination, `submit` records a confirmed no-op `destination_submission` movement attempt and then completes idempotently.
- Approval never implies final movement. Planning and Verification handoffs use their own movement purposes; only a confirmed `destination_submission` attempt satisfies final submission movement.
- Missing/invalid destination leaves the task `ready` with diagnostics and blocks movement only.

## Troubleshooting checklist

1. Save the complete JSON result and process exit status.
2. Run `dish read TASK_GID --agent AGENT` and, when an operation exists, `dish inspect OPERATION_ID --agent AGENT`.
3. Compare the reported live identity, reviewed/signed identity, placement, schema version, and legal actions.
4. For compatibility failure, confirm `DISH_HONEST_PATH`, `DISH_VERSION`, schema assets, and the exact supported protocol/schema pair.
5. For migration required, stop normal commands and ask Marco to run `dish-admin migrate`.
6. For a `started` or `uncertain` write/movement, do not retry the backend mutation. Use `dish-admin recover` after a live reread; recovery must match persisted expected/intended evidence and records the reconciliation outcome durably.
7. For tool/protocol disagreement, preserve the task unchanged and report both the protocol clause and tool rule.

The corpus migration rehearsal and live cutover remain separately authorized Step 12 work. Passing this Step 11 contract does not itself authorize production Cooking-task activation.
