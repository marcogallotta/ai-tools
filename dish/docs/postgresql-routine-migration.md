# PostgreSQL routine release migration gate

## Scope

Use this gate for any PostgreSQL-backed TEST or PROD service release after the target database has already been bootstrapped. It is deliberately separate from `dish-pg-production-prepare`, `dish-pg-production-reset`, and the offline `dish-pg-release migrate` acceptance path. Routine release migration is never run from Git merge, systemd, `ExecStartPre`, or service startup.

The source/release identity, target environment, database identity, and exact repository `ALEMBIC_HEAD` are all explicit inputs. TEST evidence never proves PROD.

## Promotion contract

For each target environment, in order:

1. resolve and check out the exact reviewed release/source commit;
2. run `scripts/dish-pg-migrate --check` against the explicit target database and retain its evidence file;
3. when the result is `pending`, execute the separately reviewed/authorized `--apply` for the same environment, database identity, source commit, and expected repository head;
4. run a fresh `--check` and require the target to report exactly the repository `ALEMBIC_HEAD`;
5. only then perform the separately authorized restart/promotion for that environment.

If any migration or verification step fails, do not restart/promote the service. Do not downgrade automatically. Preserve the environment, expected/observed database identity, source commit, before/final revision(s), expected revision, mutation state, failure rule, and next action from the redacted evidence file.

## TEST

Use a distinct owner-only evidence file for each invocation:

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
.venv/bin/python scripts/dish-pg-migrate \
  --environment test \
  --database-url "$DISH_PG_DATABASE_URL" \
  --expected-database-name "$DISH_PG_EXPECTED_DATABASE_NAME" \
  --source-commit "$SOURCE_COMMIT" \
  --evidence-file "/secure/evidence/test-pg-migration-$SOURCE_COMMIT-check.json" \
  --check
```

`result=already_current` is a deterministic no-op success. `result=pending` is a mutation-free preflight success only; it is not permission to restart. Review/authorize the routine migration, run the same exact binding with `--apply` into a new evidence file, then run a fresh `--check` into a third evidence file.

TEST refuses a production-shaped identity before mutation.

## PROD

Production mutation authority remains Marco-only. A production apply requires the explicit production environment and an exact human confirmation equal to the expected database name:

```sh
SOURCE_COMMIT="$(git rev-parse HEAD)"
.venv/bin/python scripts/dish-pg-migrate \
  --environment production \
  --database-url "$DISH_PG_DATABASE_URL" \
  --expected-database-name "$DISH_PG_EXPECTED_DATABASE_NAME" \
  --source-commit "$SOURCE_COMMIT" \
  --evidence-file "/secure/evidence/prod-pg-migration-$SOURCE_COMMIT-apply.json" \
  --apply \
  --confirm-database-name "$DISH_PG_EXPECTED_DATABASE_NAME"
```

A fresh production `--check` must still prove the exact expected head before any separately authorized production restart/promotion.

## Fail-closed conditions and evidence

The routine command validates repository migration heads against `dish_pg.release.ALEMBIC_HEAD`, inspects the database's current Alembic revision(s) before mutation, and applies only to that exact expected head. It fails before mutation for the wrong database identity, TEST pointed at production-shaped identity, missing/multiple heads, known divergent revision, ahead/foreign/unexpected revision, or repository-head inconsistency. After apply it re-reads the database and requires the exact expected identity and head.

Evidence is an exclusive owner-only, append-only JSON-lines journal and never contains a database URL or credentials. Immediately before Alembic is invoked, it durably appends `mutation_attempted=true`, `mutation_occurred=null`, and `result=apply_in_progress`; this prevents an interrupted apply from being mistaken for a clean preflight or destroying the last durable record during a later update. At most three bounded records are written per invocation. The final line is the completed state when the command reaches one.

## Startup safety net

Startup validation remains read-only and fail closed. PostgreSQL-backed service units use exit status 78 for deterministic PostgreSQL schema/identity/configuration refusal and list it in `RestartPreventExitStatus`; transient PostgreSQL unavailability remains exit status 1 and stays eligible for `Restart=on-failure`. The shadow-worker systemd units invoke `dish_pg.shadow_worker_entrypoint` as their main process so the service manager can classify stale-schema and wrong-identity refusal without putting schema mutation in startup.
