# Database backend dark launch

Status: not started. Tracks the specific path to a live dark launch; a focused subset of
`database-backend-imp.md`, not a replacement for it.

Role: dark launch means PostgreSQL passively captures a real, accumulating copy of legacy
command activity via the shadow envelope, with zero external effects — SQLite/Asana remain the
sole live-mutation path throughout. This is not cutover, activation, or writer-fencing engagement;
those stay governed by `database-backend-migration.md` and `database-backend-stage6-runbook.md`
and are out of scope here.

## Build work (ChatGPT paving)

| Work | Effort |
| --- | ---: |
| Legacy command-completion capture wiring | Hard |
| Durable local shadow spool and retry path | Hard |
| Shadow execution worker (new — distinct from `projection_worker.py`/`reconciliation_worker.py`) | Hard |
| No-external-effects enforcement | Medium |
| Per-command shadow eligibility/treatment registry | Medium |
| Dark-launch kill switch and capture-only mode | Medium |
| Backlog, lag, mismatch, and gap reporting | Medium |
| Deployment units and configuration templates | Medium |

## Host-side work (Claude/Codex on this host)

| Task | Effort |
| --- | ---: |
| Review the wiring against the authoritative checkout | Medium |
| Run it with the real PostgreSQL and service topology | Medium |
| Validate real paths, permissions, credentials, and units | Medium |
| Confirm the shadow path cannot reach live Asana writes | Medium |
| Fix host-specific integration failures | Medium–Hard |
| Enable and observe the dark launch | Medium |

## Documentation updates required before/at enable

- `CLAUDE.md` (project) — how agents should treat a live dark-launch instance: what's safe to
  touch, and the distinction between test, dark-launch, and production.
- `README.md` — operator entry points: how to start/stop/check status of the dark launch, and
  where its logs live.
- `architecture.md` — describe the shadow-capture worker, kill switch, and capture-only
  enforcement once built (governed by the existing "update architecture.md in the same commit"
  rule — not a separate pass).

## Explicitly not in this doc

Backup/restore/PITR rehearsal, crash/fault injection, production-shaped rehearsal, and full
cutover remain tracked in `database-backend-imp.md` and `database-backend-postgresql-test-plan.md`.
Dark launch's own capture-only purpose does not require those to land first, but cutover still
does.
