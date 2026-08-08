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
- Before an agent-created Verification Human Review hold is persisted, Dish may require a neutral escalation preflight that identifies the evidence, repairs considered, and the unresolved Marco-only choice. The preflight must not imply that legitimate Human Review is undesirable. A reasonable defensible estimate with stated assumptions is valid when an exact value is unknowable; uncertainty alone is not a blocker. If no single estimate is defensible, do not invent false precision.
- If Verification can already construct the exact governed candidate that resolves a concern, that candidate belongs in the semantic-proposal review path rather than an open-ended Human Review hold. Human Review is for the Marco-only choice that must be answered before an exact candidate can exist.
- Human-facing review presentation is intentionally compressed: decision/outcome first, quantified material consequence when applicable, then the smallest meaningful choice. For a semantic proposal, however, the normal pre-approval inspect view must still show every linked candidate change covered by approval/application; verbose mode is only for rationale, evidence/provenance, protocol mechanics, IDs, and diagnostics.
- An unanswered agent-created Verification Human Review hold may be dismissed by Marco as an invalid escalation. Dismissal preserves the original finding and reason, records the dismissal reason, always returns the unchanged candidate to fresh Verification regardless of the hold's stored resume status, and does not create a substantive Marco decision or governed authorization. Substantive Marco approval is different: it records the decision and may honor the hold's stored resume status, including returning the task to Research.
- Small governed-text edits that may be incidental cleanup require explicit agent intent before they can enter the governed proposal path. This is an intent check, not a semantic classifier: the agent must restore incidental text exactly or explicitly identify an intended governed edit.
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

For Verification Human Review, the agent first states the unresolved issue and, when challenged, the supporting basis and repair routes considered. It should use a reasonable defensible estimate with stated assumptions instead of demanding false precision, and quantify any structured threshold blocker as one defensible estimate versus the limit and excess/shortfall. If no single estimate is defensible, the agent must not invent one merely to populate the blocker structure. If the exact governed repair is already knowable, Verification proposes that exact candidate through the semantic-proposal path. Only when a genuine Marco-only choice remains before such a candidate can exist does Dish park the exact held cycle. Marco then reviews the held item through the review queue, where approval records the substantive decision and follows the stored resume route while rejection dismisses the unanswered escalation itself as invalid and always returns the unchanged candidate to fresh Verification.

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
