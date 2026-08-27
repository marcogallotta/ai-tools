# Lifecycle V4 zero-turn webhook activation — historical

> **NEVER ACTIVATED — historical/reference only.**
>
> Lifecycle V4 was built as an experimental continuation of the unused PR lifecycle dispatcher, but
> it was never deployed, commissioned, or used and will not be activated. The service, webhook,
> daemon, ledger, environment variables, and commands below are not current infrastructure or an
> operating procedure. Present-tense descriptions record intended prototype behavior only.

Lifecycle V4 was designed to add an event-driven **wake** seam around the proposed lifecycle reread without adding a writer, lifecycle engine, semantic queue, or model heartbeat. The prototype `scripts/pr_lifecycle_v4.py` would have treated authenticated GitHub/Asana deliveries only as dirty-resource hints, coalesced duplicate/stale/out-of-order deliveries, and re-read GitHub/Asana before any actionable decision.

The proposed V4 semantic identity was `actionable_version`, separate from V3 `case_key` / `evidence_fingerprint`. It would have bound repository + reason class + exact PR/task/head + normalized semantic evidence + exact next owner/action + wake-policy generation while excluding retry/backoff/timestamp noise. Phase B and Coordinator wake output were never activated.

Commissioning was never performed. The proposed requirement to call `V4Reconciler.baseline_current()` against a new empty V4 ledger remains historical design detail, not a current setup step.

The design would have coalesced new actionable versions for one owner into one bounded wake packet, used one dedicated lifecycle Codex app-server thread and a host-side OS fence, resumed `notLoaded` threads, and admitted a turn only after an idle readback. No such dedicated thread or fence is commissioned.

The proposed receipt journal was `PREPARED -> SUBMITTED -> ACCEPTED -> COMPLETED`, with `AMBIGUOUS` for lost acceptance readback. Its recovery design required persisted-thread readback before replay and would have forbidden blind retries. No lifecycle receipt ledger currently exists.

The proposed webhook ingress required provider authentication before `V4StateStore.mark_dirty` and an authenticated public ingress or trusted relay. Neither was commissioned. Repository tests may still exercise the retained prototype source, but that does not establish an active webhook subscription or runtime.

## Historical service entrypoint

`scripts/pr_lifecycle_v4_service.py` was the proposed HTTP/runtime adapter and remains unactivated
reference code. The proposed host installation would have provided systemd, reverse-proxy,
credential, and state-path configuration while invoking this repository file directly.
The proposed entrypoint would have owned only transport composition; it never became lifecycle or
writer authority.

The following host-runner configuration was never installed and must not be used as a current procedure:

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

The design required credentials and webhook secrets outside Git, mode-`0600` secret files, a
`GET /healthz` endpoint, and local report/audit files. None of those paths should be assumed to
exist on an operator host.

The prototype proposed starting a Codex daemon through `tools/dish-integrator-daemon start`. That
wrapper and its isolated-home/read-only design remain historical implementation detail; they do not
identify a running service.

## Historical persistent interactive Integrator

The prototype was designed to connect by WebSocket to the Unix socket of a dedicated Integrator
Codex daemon. No standing daemon or persistent lifecycle thread exists.
The proposed socket path was
`$DISH_LIFECYCLE_V4_STATE_DIR/codex-home/app-server-control/app-server-control.sock`.

The following proposed installation command was never commissioned:

```sh
dish-integrator open
```

The proposed command would have started the daemon and bridge services, acquired the same
`integrator.fence` used by `WakeBridge`, and opens the exact thread from `thread.json` through
`codex resume --remote unix://`. While the TUI owns the fence, webhooks continue to coalesce and
reconcile without a model turn; pending actionable versions wait for the fence. Exiting the TUI
releases the fence and permits the bridge to deliver pending work. Direct `codex resume` without
this wrapper is unsupported because it bypasses the shared admission fence.

The proposed wrapper commands and service-name overrides were never installed as current user
services.

The design would have left `.github/workflows/full-regression.yml` as the nightly semantic owner
and consumed its durable evidence without scheduling a second path. No Integrator V4 consumer
currently reads or routes those results.
