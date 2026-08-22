# Lifecycle V4 zero-turn webhook activation

Lifecycle V4 adds an event-driven **wake** seam around the existing authoritative lifecycle reread; it does not add a writer, lifecycle engine, semantic queue, or model heartbeat. `scripts/pr_lifecycle_v4.py` treats authenticated GitHub/Asana deliveries only as dirty-resource hints. Duplicate, stale, and out-of-order deliveries coalesce durably, then the existing lifecycle projection is reread from GitHub/Asana before any actionable decision. Compare-and-clear tokens prevent an event arriving during reconciliation from being lost.

The V4 semantic identity is `actionable_version`, separate from V3 `case_key` / `evidence_fingerprint`. It binds repository + reason class + exact PR/task/head + normalized semantic evidence + exact next owner/action + wake-policy generation while excluding retry/backoff/timestamp noise. Phase B activates only exact `next_owner == Integrator`; Coordinator wake output remains dormant until the same seam is independently proven. No new actionable version means **zero model turns**.
Workflow run, attempt, job, and delivery occurrence fields remain in the packet/receipt for correlation
but are excluded from the semantic hash; changing only those fields cannot admit another wake.

Before enabling the model bridge against a new empty V4 ledger, commissioning must call `V4Reconciler.baseline_current()` exactly once. That authoritative reread records the current Integrator-owned actionable versions without preparing receipts or touching the Codex thread. The baseline is idempotent and fails closed if wake history already exists; subsequent new actionable versions follow the normal fenced wake path, while unchanged baseline versions produce zero turns. Do not substitute a forced first reconciliation for this cold-start baseline.

New actionable versions for one owner coalesce into one bounded wake packet. Delivery uses one dedicated lifecycle Codex app-server thread and a host-side OS fence. A persisted thread that reports `thread/read` status `notLoaded` is explicitly resumed via `thread/resume` in the same app-server process — at client construction for the configured lifecycle thread, and defensively again immediately before admission — before it can be admitted. Immediately before `turn/start`, `thread/read` must report `status.type == idle`; an active/human-owned/not-yet-resumed turn causes zero launch. Every start uses `clientUserMessageId = wake_id`. Each case in a wake packet carries required provenance for the receiver — `case_key`, `reviewed_head`, `review_verdict`, `evidence_fingerprint`, and normalized semantic `evidence` (volatile timing/retry keys excluded) — alongside repository/PR/task/head identity; the receiver still re-reads live GitHub/Asana authority before acting.

The durable receipt journal is `PREPARED -> SUBMITTED -> ACCEPTED -> COMPLETED`, with `AMBIGUOUS` for lost acceptance readback. An ambiguous or submitted-but-unconfirmed receipt is never blindly replayed: recovery performs `thread/read(includeTurns=true)` and searches persisted `userMessage.clientId`. A found marker moves the receipt to `ACCEPTED`. An absent marker permits a retry back to `PREPARED` only when the thread is also mechanically proven `idle`; while the thread is active/unknown/not-loaded, absence of a marker is not proof of non-acceptance and the receipt stays `AMBIGUOUS` with zero replay. For each exact `ACCEPTED` turn, a bounded observer resumes the dedicated thread, checks persisted turn state once, and then waits for app-server's `turn/completed` notification. At that boundary one exact persisted-thread readback writes `COMPLETED` and the structured proposal is added to the audit. After a process restart, the same observer first reads persisted thread history, so a completion that happened while the service was down is recovered without replaying the turn or polling idle state.

Webhook ingress must validate provider authentication before `V4StateStore.mark_dirty`: GitHub `X-Hub-Signature-256` HMAC-SHA256 and Asana `X-Hook-Signature` HMAC-SHA256 are supported helpers. Deployment must provide an authenticated public ingress or equivalent trusted webhook relay; if it cannot, commissioning remains **BLOCKED** rather than substituting polling/model heartbeats. The repository source can be exercised without commissioning by feeding verified payloads to `ingest_event()` and wiring `V4Reconciler.authoritative_cases` to the existing lifecycle projection.
The GitHub ingress subscription or trusted relay must include completed `workflow_run` deliveries so the existing scheduled Full regression can dirty the repository resource after its durable artifact has been uploaded.

## Repository-owned service entrypoint

`scripts/pr_lifecycle_v4_service.py` is the maintained HTTP/runtime adapter. Host installation may
provide systemd, reverse-proxy, credential, and state-path configuration, but must invoke this
repository file directly; do not copy or modify a second Python service under `~/.local/lib`.
The entrypoint owns only transport composition: authenticated webhook handling, the existing
authoritative projection reread, the V4 state/receipt store, the Integrator-only bridge, health
reporting, and startup reconciliation. It does not add lifecycle or writer authority.

The host runner supplies credentials without printing them and sets these bounded values:

```sh
export DISH_LIFECYCLE_V4_REPO=/home/marco/ai-tools
export DISH_LIFECYCLE_V4_STATE_DIR=/home/marco/.local/state/dish/pr-lifecycle-v4
export DISH_LIFECYCLE_V4_STATE_PATH="$DISH_LIFECYCLE_V4_STATE_DIR/state.json"
export DISH_LIFECYCLE_V4_PROJECTION=/home/marco/.local/state/dish/pr-lifecycle/lifecycle.json
export DISH_LIFECYCLE_V4_PYTHON=/home/marco/ai-tools/dish/.venv/bin/python
export DISH_LIFECYCLE_V4_CODEX=/home/marco/.codex/packages/standalone/current/codex
export DISH_INTEGRATOR_CODEX_HOME="$DISH_LIFECYCLE_V4_STATE_DIR/codex-home"
export DISH_LIFECYCLE_V4_APP_SERVER_SOCKET="$DISH_INTEGRATOR_CODEX_HOME/app-server-control/app-server-control.sock"
export DISH_LIFECYCLE_V4_BASELINE_ON_START=1
export DISH_LIFECYCLE_V4_WAKE_ENABLED=1
exec "$DISH_LIFECYCLE_V4_PYTHON" \
  /home/marco/ai-tools/scripts/pr_lifecycle_v4_service.py \
  --bind 127.0.0.1 --port 8797
```

Keep GitHub and Asana credentials and webhook secrets outside Git. The service creates webhook
secret files mode `0600` beneath the configured state directory. `GET /healthz` reports the exact
repository source root, state path, thread id/status, dirty count, and process-lifetime counters.
The observe-only Integrator consumer writes `integrator-report.json` and rotated
`integrator-audit.ndjson` in that same directory. These consume the lifecycle projection's canonical
CI ownership, causal fingerprint, and repair owner; they never classify a failure or create a second
wake identity. `dish-integrator report` shows the latest decision snapshot and `dish-integrator audit`
shows the latest structured evidence. Missing or contradictory canonical projection evidence is logged
and suppressed with zero model turns until an authoritative reread resolves it.

The Codex daemon must be started through `tools/dish-integrator-daemon start`, with the matching
`stop` command in `ExecStop`. That wrapper prepares an isolated Codex home, reuses only the operator's
authentication file, disables shell, web search, apps/plugins inherited from the operator home and
multi-agent tools, and configures only the repository's purpose-built `dish_integrator` read-only MCP
server. The MCP tools resolve inputs from the exact local V4 receipt ledger: the model cannot select an
arbitrary repository, PR, head or Asana task. The automated turn also has a strict observe-only result
schema and a read-only sandbox. Changing this daemon configuration, thread bootstrap, turn packet,
output schema or submission fence requires fresh commissioning of the exact runtime.

## Persistent interactive Integrator

The service connects by WebSocket to the Unix socket of the dedicated Integrator Codex daemon.
The persistent thread has one resident daemon owner and is directly resumable in the Codex TUI.
The socket defaults to `$DISH_LIFECYCLE_V4_STATE_DIR/codex-home/app-server-control/app-server-control.sock`
and is set explicitly with `DISH_LIFECYCLE_V4_APP_SERVER_SOCKET` in the host runner.

Install `tools/dish-lifecycle-v4-integrator` on the operator path as `dish-integrator`:

```sh
dish-integrator open
```

That command starts the daemon and bridge services if needed, acquires the same
`integrator.fence` used by `WakeBridge`, and opens the exact thread from `thread.json` through
`codex resume --remote unix://`. While the TUI owns the fence, webhooks continue to coalesce and
reconcile without a model turn; pending actionable versions wait for the fence. Exiting the TUI
releases the fence and permits the bridge to deliver pending work. Direct `codex resume` without
this wrapper is unsupported because it bypasses the shared admission fence.

The wrapper also provides `status`, `start`, `stop`, `restart`, `logs`, `thread`, `report`, and `audit`. Its default
service names match the current user services and may be overridden with
`DISH_LIFECYCLE_V4_DAEMON_SERVICE` and `DISH_LIFECYCLE_V4_BRIDGE_SERVICE`.

The existing `.github/workflows/full-regression.yml` schedule remains the nightly semantic owner.
Integrator does not schedule, classify, fingerprint or route a second nightly path. Its
`get_nightly_health` tool and operator report read that workflow's current result. On a completed
workflow delivery, the lifecycle projection verifies and consumes the workflow's exact durable
evidence artifact. The exact run/artifact evidence is reused from the prior atomic projection so the
full diagnostic bundle is downloaded only once per new run. A typed failure with no completed triage becomes an Integrator-owned V4 case using
the existing causal fingerprint; clean evidence emits no case. Replaying the same artifact reuses the
same V4 `actionable_version`, so clean, unchanged, and duplicate nightly deliveries cost zero model
turns.
