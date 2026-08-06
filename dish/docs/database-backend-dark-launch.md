# Database backend dark launch

Status: build work complete and safety-verified (independently reviewed, initial gap found and
fixed, re-verified SAFE — see `database-backend-production-change-ledger.md` for the fix
commits). Runtime wiring rehearsal and enablement remain outstanding. Tracks the specific path to
a live dark launch; a focused subset of `database-backend-imp.md`, not a replacement for it.

Role: dark launch means PostgreSQL passively captures a real, accumulating copy of legacy
command activity via the shadow envelope, with zero external effects — SQLite/Asana remain the
sole live-mutation path throughout. This is not cutover, activation, or writer-fencing engagement;
those stay governed by `database-backend-migration.md` and `database-backend-stage6-runbook.md`
and are out of scope here.

## Build work (ChatGPT paving) — done

| Work | Effort |
| --- | ---: |
| Legacy command-completion capture wiring | Hard |
| Durable local shadow spool and retry path | Hard |
| Shadow execution worker (new — distinct from `projection_worker.py`/`reconciliation_worker.py`) | Hard |
| No-external-effects enforcement | Medium — first pass had a real gap (shadow-origin outbox rows were claimable whenever the epoch flag was `true`); fixed with a DB-enforced `live`/`shadow` origin tag and an unconditional `claim_next` exclusion, independently re-verified SAFE |
| Per-command shadow eligibility/treatment registry | Medium |
| Dark-launch kill switch and capture-only mode | Medium — first pass only stopped new capture, not the worker draining already-spooled/claimed envelopes; fixed |
| Backlog, lag, mismatch, and gap reporting | Medium |
| Deployment units and configuration templates | Medium |
| Production-safe legacy location-manifest capture | Medium — explicit production identity, read-only SQLite and Asana task reads, fail-closed mixed-identity checks, and owner-only atomic output are implemented; live execution remains outstanding |

## Host-side work (Claude/Codex on this host)

| Task | Effort | Status |
| --- | ---: | --- |
| Review the wiring against the authoritative checkout | Medium | Done |
| Confirm the shadow path cannot reach live Asana writes | Medium | Done — independently reviewed twice (initial gap found, fix verified) |
| Run it with the real PostgreSQL and service topology | Medium | Outstanding |
| Validate real paths, permissions, credentials, and units | Medium | Outstanding |
| Fix host-specific integration failures | Medium–Hard | Outstanding (depends on the rehearsal above) |
| Enable and observe the dark launch | Medium | Outstanding — Marco-gated |

## Documentation updates required before/at enable — done

- `CLAUDE.md` (top-level, `ai-tools/CLAUDE.md`) — how agents should treat a live dark-launch
  instance: read-only evidence, never production authority; what's safe to touch (status checks)
  versus Marco-only (mode, kill switch, service lifecycle).
- `dish/README.md` — operator entry points: how to start/stop/check status of the dark launch.
- `dish/docs/architecture.md` — describes the shadow-capture worker, kill switch, and
  capture-only enforcement.
- `dish/docs/database-backend-dark-launch-runbook.md` — new operator runbook with the full
  prepare/enable/disable command sequence; this tracker doc stays the status/effort summary, the
  runbook is the how-to.

## Explicitly not in this doc

Backup/restore/PITR rehearsal, crash/fault injection, production-shaped rehearsal, and full
cutover remain tracked in `database-backend-imp.md` and `database-backend-postgresql-test-plan.md`.
Dark launch's own capture-only purpose does not require those to land first, but cutover still
does.
