# Workflow and human review

## Read this when

Read this when changing workflow legality, verification, holds, semantic proposals, Human Review, approval/application, or recovery continuations.

## Scope

This document records workflow semantics and authority. It does not dictate that HTTP, CLI, renderers, adapters, or persistence helpers contain no logic.

## Authoritative implementation

Current anchors include `dish_tool/application_service.py`, `dish_tool/workflow_policy.py`, workflow step/use-case modules, `dish_tool/semantic_proposals.py`, `dish_tool/review_queue.py`, and `dish_tool/admin.py`.

## Actors, processes, and stores

Agents perform workflow work; Human Review authorizes governed proposals/decisions; service/admin surfaces expose available continuations; SQLite currently stores workflow evidence.

## Authority and data ownership

The authoritative workflow decision answers whether a consequential transition is legal for the current durable/live facts. Consumers may present, suppress, group, translate, or authorization-filter that result and may expose unrelated surface actions; they must not independently authorize a contradictory workflow transition.

Human approval is the authorization for the exact governed changes in an approved proposal. Applying that already-approved proposal is a later mutation step, not a second human-authorization decision for those same changes.

## Invariants

- Proposal approval and proposal application are separate durable actions.
- Human approval binds the exact proposal/governed-change bundle presented for review.
- Application of that exact approved bundle must not require a second human authorization for the same governed changes.
- Application may revalidate proposal identity, integrity, applicability, cycle/content binding, current authoritative facts, and concurrency before mutating.
- Application must not silently broaden, substitute, or alter the approved bundle.
- Candidate proposal content does not become canonical merely by being proposed or approved.
- Proposal validity/actionability is evaluated against governed semantic facts rather than irrelevant cosmetic/task metadata.
- Verification/review evidence stays bound to the exact subject it reviewed.
- Human Review and recovery continuations target durable recorded work rather than an ambiguous nearby operation.
- "Allowed actions" are a derived view of authority, not a second state machine.

## Process and transaction boundaries

Workflow policy may be evaluated in shared application/domain code while transports, renderers, persistence queries, and adapters perform their own legitimate responsibilities. The boundary is authority, not the presence or absence of conditionals.

## Normal flow

For semantic proposals, the durable lifecycle is:

1. create and validate the exact proposal against the governed semantic subject;
2. obtain human approval for that exact proposal;
3. retain the approved object without changing canonical task content;
4. later claim/application work rereads and revalidates the same approved proposal against current authoritative facts;
5. apply that proposal unchanged, or fail/reconcile if it is no longer applicable.

More generally, workflow execution reads authoritative state, derives legal transitions, executes the selected transition through the owning application path, persists its outcome/evidence, then exposes resulting state/actions to callers.

## Failure, replay, recovery, and concurrency

Stale proposals, mismatched reviewed content, changed proposal identity/integrity, conflicting targets, or lost claims fail closed or move through explicit recovery. If the exact approved candidate is already canonical, recovery may reconcile that fact rather than demanding another human approval or blindly writing again.

Exact recovery mechanisms may evolve; durable identity, authorization, and no-silent-broadening guarantees are architectural, not today's function layout.

## Change routing

Change workflow legality at the authority that decides it. Change transport-specific guidance, presentation, argument collection, or recovery explanation in the relevant surface. Persistence helpers may legitimately query/shape required facts; they should not independently become workflow policy authority.

## Proving tests

Relevant evidence includes semantic proposal/Human Review workflow tests, workflow policy tests, connected-agent surface tests, and recovery/lease tests. Favor end-to-end behavioral agreement across surfaces over AST/source-text topology checks. Structural tests remain appropriate where a structural boundary itself protects authority or exposure.

## Current debt and temporary compatibility

Legacy workflow, PostgreSQL target workflow, and migration-era recovery paths still overlap. Stage B is expected to consolidate them further. Current recovery mechanics should be documented as current behavior unless backed by an explicit accepted decision.

## Related documents

- [Commands and surfaces](commands-and-surfaces.md)
- [Operations, leases, and fencing](operations-leases-and-fencing.md)
- [ADR-0003](decisions/0003-approval-and-application-are-separate.md)
