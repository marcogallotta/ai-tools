# Frontend pre-database handoff

Status: complete for the current pre-integration checkpoint.

The frontend can be visually reviewed now, but real authentication and canonical board reads remain
correctly blocked. The next meaningful work begins when the database rollout has an exact final head.

## What to record immediately after the database rollout

1. exact activated Alembic head and schema fingerprint;
2. exact source commit/release and production-change ledger closure, with no `WORKTREE` marker;
3. whether the Stage 2 security support tables landed with the rollout or require the next migration;
4. the independently current restore-fence mechanism and location;
5. accepted answers to the six migration-shape decisions in
   `frontend-db-migration-reconciliation.md`;
6. indexes actually present and the first representative query plans.

## First post-rollout sequence

- rerun the Gate B schema reconciliation against the activated database;
- finish the Stage 2 security migration/fence and obtain independent Gate A review;
- implement Stage 2 in packages 2A–2D from `frontend-stage2-implementation-checklist.md`;
- deploy Stage 2 to the test origin using `frontend-test-deployment-readiness.md`;
- obtain functional login/session feedback;
- obtain Gate B Stage 3 review, then implement packages 3A–3D;
- obtain the next visual review using real board data before Stage 4 detail work.

## Current stop point

Do not add more visual prototype polish, fake authentication, compatibility reads from the old
schema, or guessed attention semantics. The pre-database work is now sufficient to start Stage 2/3
without rediscovering ownership, acceptance cases, or migration gaps.
