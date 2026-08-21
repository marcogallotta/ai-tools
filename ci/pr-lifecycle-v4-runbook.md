# Lifecycle V4 zero-turn webhook activation

Lifecycle V4 adds an event-driven **wake** seam around the existing authoritative lifecycle reread; it does not add a writer, lifecycle engine, semantic queue, or model heartbeat. `scripts/pr_lifecycle_v4.py` treats authenticated GitHub/Asana deliveries only as dirty-resource hints. Duplicate, stale, and out-of-order deliveries coalesce durably, then the existing lifecycle projection is reread from GitHub/Asana before any actionable decision. Compare-and-clear tokens prevent an event arriving during reconciliation from being lost.

The V4 semantic identity is `actionable_version`, separate from V3 `case_key` / `evidence_fingerprint`. It binds repository + reason class + exact PR/task/head + normalized semantic evidence + exact next owner/action + wake-policy generation while excluding retry/backoff/timestamp noise. Phase B activates only exact `next_owner == Integrator`; Coordinator wake output remains dormant until the same seam is independently proven. No new actionable version means **zero model turns**.

Before enabling the model bridge against a new empty V4 ledger, commissioning must call `V4Reconciler.baseline_current()` exactly once. That authoritative reread records the current Integrator-owned actionable versions without preparing receipts or touching the Codex thread. The baseline is idempotent and fails closed if wake history already exists; subsequent new actionable versions follow the normal fenced wake path, while unchanged baseline versions produce zero turns. Do not substitute a forced first reconciliation for this cold-start baseline.

New actionable versions for one owner coalesce into one bounded wake packet. Delivery uses one dedicated lifecycle Codex app-server thread and a host-side OS fence. A persisted thread that reports `thread/read` status `notLoaded` is explicitly resumed via `thread/resume` in the same app-server process — at client construction for the configured lifecycle thread, and defensively again immediately before admission — before it can be admitted. Immediately before `turn/start`, `thread/read` must report `status.type == idle`; an active/human-owned/not-yet-resumed turn causes zero launch. Every start uses `clientUserMessageId = wake_id`. Each case in a wake packet carries required provenance for the receiver — `case_key`, `reviewed_head`, `review_verdict`, `evidence_fingerprint`, and normalized semantic `evidence` (volatile timing/retry keys excluded) — alongside repository/PR/task/head identity; the receiver still re-reads live GitHub/Asana authority before acting.

The durable receipt journal is `PREPARED -> SUBMITTED -> ACCEPTED -> COMPLETED`, with `AMBIGUOUS` for lost acceptance readback. An ambiguous or submitted-but-unconfirmed receipt is never blindly replayed: recovery performs `thread/read(includeTurns=true)` and searches persisted `userMessage.clientId`. A found marker moves the receipt to `ACCEPTED`. An absent marker permits a retry back to `PREPARED` only when the thread is also mechanically proven `idle`; while the thread is active/unknown/not-loaded, absence of a marker is not proof of non-acceptance and the receipt stays `AMBIGUOUS` with zero replay. Terminal completion is reconstructed the same way rather than by consuming live `turn/completed` notifications: an `ACCEPTED` receipt is reconciled on every dispatch pass by reading the matching turn's own status from persisted thread history, and only a mechanically observed `completed` status writes the durable `COMPLETED` receipt — this is crash-safe because it never depends on the originating process still being alive, and it distinguishes still-in-flight `ACCEPTED` from terminal `COMPLETED` using only durable state.

Webhook ingress must validate provider authentication before `V4StateStore.mark_dirty`: GitHub `X-Hub-Signature-256` HMAC-SHA256 and Asana `X-Hook-Signature` HMAC-SHA256 are supported helpers. Deployment must provide an authenticated public ingress or equivalent trusted webhook relay; if it cannot, commissioning remains **BLOCKED** rather than substituting polling/model heartbeats. The repository source can be exercised without commissioning by feeding verified payloads to `ingest_event()` and wiring `V4Reconciler.authoritative_cases` to the existing lifecycle projection.

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
export DISH_LIFECYCLE_V4_STATE_PATH="$DISH_LIFECYCLE_V4_STATE_DIR/state-commissioned.json"
export DISH_LIFECYCLE_V4_PROJECTION=/home/marco/.local/state/dish/pr-lifecycle/lifecycle.json
export DISH_LIFECYCLE_V4_PYTHON=/home/marco/ai-tools/dish/.venv/bin/python
export DISH_LIFECYCLE_V4_CODEX=/home/marco/.codex/packages/standalone/current/codex
export DISH_LIFECYCLE_V4_APP_SERVER_COMMAND='/home/marco/.codex/packages/standalone/current/codex app-server proxy'
export DISH_LIFECYCLE_V4_BASELINE_ON_START=1
export DISH_LIFECYCLE_V4_WAKE_ENABLED=1
exec "$DISH_LIFECYCLE_V4_PYTHON" \
  /home/marco/ai-tools/scripts/pr_lifecycle_v4_service.py \
  --bind 127.0.0.1 --port 8797
```

Keep GitHub and Asana credentials and webhook secrets outside Git. The service creates webhook
secret files mode `0600` beneath the configured state directory. `GET /healthz` reports the exact
repository source root, state path, thread id/status, dirty count, and process-lifetime counters.

## Persistent interactive Integrator

The service connects through `codex app-server proxy` to the already-managed shared Codex daemon.
It does not spawn a second private app-server. The persistent thread therefore has one resident
daemon owner and is also directly resumable in the Codex TUI.

Install `tools/dish-lifecycle-v4-integrator` on the operator path or invoke it from the checkout:

```sh
tools/dish-lifecycle-v4-integrator open
```

That command starts the daemon and bridge services if needed, acquires the same
`integrator.fence` used by `WakeBridge`, and opens the exact thread from `thread.json` through
`codex resume --remote unix://`. While the TUI owns the fence, webhooks continue to coalesce and
reconcile without a model turn; pending actionable versions wait for the fence. Exiting the TUI
releases the fence and permits the bridge to deliver pending work. Direct `codex resume` without
this wrapper is unsupported because it bypasses the shared admission fence.

The wrapper also provides `status`, `start`, `stop`, `restart`, `logs`, and `thread`. Its default
service names match the current user services and may be overridden with
`DISH_LIFECYCLE_V4_DAEMON_SERVICE` and `DISH_LIFECYCLE_V4_BRIDGE_SERVICE`.
