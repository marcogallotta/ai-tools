# Database backend dark launch

Status: live in production — preflight passed, host installed, capture and shadow execution both
enabled. Tracks the specific path to a live dark launch; a focused subset of
`database-backend-imp.md`, not a replacement for it. Day-to-day operation and investigation belong
in `database-backend-dark-launch-runbook.md`, not here.

Role: dark launch means PostgreSQL passively captures a real, accumulating copy of legacy
command activity via the shadow envelope, with zero external effects — SQLite/Asana remain the
sole live-mutation path throughout. This is not cutover, activation, or writer-fencing engagement;
those stay governed by `database-backend-migration.md` and `database-backend-stage6-runbook.md`
and are out of scope here.

## Explicitly not in this doc

Backup/restore/PITR rehearsal, crash/fault injection, production-shaped rehearsal, and full
cutover remain tracked in `database-backend-imp.md` and `database-backend-postgresql-test-plan.md`.
Dark launch's own capture-only purpose does not require those to land first, but cutover still
does.
