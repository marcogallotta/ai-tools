# ADR: Approval and application are separate

Status: Accepted

## Read this when

Read this when changing semantic proposals, Human Review, approval, `apply-proposal`, proposal recovery, or governed-change execution.

## Scope

Human approval authorizes the exact governed changes contained in a proposal. Applying those already-approved changes is a later mutation step.

## Authoritative implementation

Current implementation anchors live in [Workflow and human review](../workflow-and-human-review.md). Exact module locations may change without changing this decision.

## Actors, processes, and stores

A proposal producer creates a candidate; Human Review authorizes the exact proposal; a separate durable application action then claims and executes that already-authorized object against fresh authoritative facts. In the normal operator path Dish performs that mechanical application immediately after durable approval persistence; a second AI agent is not required merely to execute the already-settled bundle.

## Authority and data ownership

The durable approved proposal owns the authorized change bundle. Canonical task state changes only during application, not merely because approval exists.

## Invariants

- Approval binds the exact proposal/change bundle presented for review.
- Applying unchanged approved changes must not require a second human authorization.
- Approval and application remain separately durable/auditable even when one admin command performs them sequentially.
- Mechanical application does not require an AI agent to reinterpret or re-decide the proposal.
- Application may revalidate proposal identity, integrity, applicability, cycle/content binding, current facts, and concurrency.
- Application must not broaden, substitute, or alter the approved bundle.
- Candidate content does not become canonical before application.

## Process and transaction boundaries

Approval persistence and mutation execution are separate durable actions and may occur in different processes or transactions; the exact approved object must remain identifiable across that boundary. The normal admin path may invoke the second action immediately after the first without collapsing their persistence or authority semantics.

## Normal flow

Create and validate a proposal, obtain and durably persist human approval for that exact object, then mechanically claim, revalidate, and apply the same approved changes unchanged. Normally Dish performs the application immediately after approval; if it cannot complete safely, the approved object remains durable for explicit retry/recovery rather than requiring reapproval.

## Failure, replay, recovery, and concurrency

If the proposal is malformed, obsolete, changed, or no longer applicable, fail or recover without mutating a different bundle. If the exact approved candidate is already canonical, recovery may reconcile that fact without reapproval or another blind write.

## Change routing

Refactors may change the modules that create, review, claim, or apply proposals. Any change that adds a second human authorization for the same approved bundle or permits application of a different bundle changes this decision.

## Proving tests

Tests should cover proposal validity at creation and approval, approval/application separation, no candidate leakage before apply, exact revalidation, already-live recovery, and rejection of stale or altered bundles.

## Current debt and temporary compatibility

Current legacy/PostgreSQL/agent surfaces may expose different parts of the lifecycle while migration is underway; none may weaken the exact-approval contract.

## Related documents

- [Workflow and human review](../workflow-and-human-review.md)
- [Commands and surfaces](../commands-and-surfaces.md)
