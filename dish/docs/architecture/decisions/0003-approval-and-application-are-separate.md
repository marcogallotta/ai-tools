# ADR-0003: Approval and application are separate

Status: Accepted

## Read this when
Changing semantic proposals, Human Review, approval, or later application of an approved change.

## Scope
This decision owns asynchronous proposal approval/application separation.

## Authoritative implementation
`dish_tool/semantic_proposals.py`, `dish_tool/review_queue.py`, `dish_tool/admin.py`, `dish_tool/commands.py`.

## Actors, processes, and stores
An agent proposes; Marco/admin reviews; an eligible later agent may apply the exact approved bundle.

## Authority and data ownership
Approval records authorization for one exact proposal/version. Application is a separate governed mutation with its own actor/run/request evidence.

## Invariants
Approval never silently mutates the live task; application cannot alter or broaden the approved bundle.

## Process and transaction boundaries
Review settlement and later application are separate durable executions.

## Normal flow
Queue proposal, inspect, approve/reject, then apply the exact approved proposal.

## Failure, replay, recovery, and concurrency
Claims and proposal status prevent double application; replay returns the settled result.

## Change routing
Do not collapse review approval into immediate Asana/workflow mutation.

## Proving tests
`tests/test_semantic_proposal_bundle_workflow.py`, `tests/test_human_review_queue_workflow.py`.

## Current debt and temporary compatibility
The PostgreSQL target command registry still marks proposal application/review capture-only during dark launch.

## Related documents
[Workflow and human review](../workflow-and-human-review.md), [Dark launch](../dark-launch.md).
