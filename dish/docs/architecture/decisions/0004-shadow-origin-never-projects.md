# ADR-0004: Shadow-origin work never projects

Status: Accepted

## Read this when
Changing shadow execution, outbox origin, projection worker eligibility, or effect-enable controls.

## Scope
This decision owns structural isolation between shadow commands and Asana effects.

## Authoritative implementation
`dish_pg/stage5_models.py`, `dish_pg/transition.py`, `dish_pg/projection_worker.py`, `dish_pg/shadow_worker.py`.

## Actors, processes, and stores
Shadow worker writes PostgreSQL target evidence; projection worker is the only target external-effect process.

## Authority and data ownership
Outbox origin is immutable. Shadow-origin rows are evidence and never eligible for external dispatch.

## Invariants
Projection workers reject shadow origin independently of projection-epoch effect configuration.

## Process and transaction boundaries
Shadow command transactions may emit internal intents, but worker claim/admission refuses them before adapter calls.

## Normal flow
Shadow execute, record comparison, retain internal projection intent only as evidence.

## Failure, replay, recovery, and concurrency
Misconfiguration of an epoch cannot make shadow work dispatchable; stale workers remain fenced by origin and claim identity.

## Change routing
Do not weaken origin checks to simplify acceptance tests or dark-launch setup.

## Proving tests
`tests/postgresql/test_dark_launch_policy.py`, `tests/postgresql/test_dark_launch_shadow_worker.py`, `tests/postgresql/test_projection_attempt_lifecycle.py`.

## Current debt and temporary compatibility
The projection worker is not yet a production deployable in the current authority topology.

## Related documents
[Dark launch](../dark-launch.md), [External effects and Asana](../external-effects-and-asana.md).
