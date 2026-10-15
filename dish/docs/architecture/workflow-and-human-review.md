# Workflow and human review

## Read this when

Read this when changing workflow legality, verification, holds, semantic proposals, Human Review, approval/application, or recovery continuations.

## Scope

This document records workflow semantics and authority. It does not dictate that HTTP, CLI, renderers, adapters, or persistence helpers contain no logic.

## Authoritative implementation

Current anchors include `dish_tool/application_service.py`, `dish_tool/workflow_policy.py`, workflow step/use-case modules, `dish_tool/semantic_proposals.py`, `dish_tool/review_queue.py`, and `dish_tool/admin.py`.

## Actors, processes, and stores

Agents perform workflow work; current connected-agent Marco choices are recorded as attributed
`Human — Marco:` Decisions and may attest the exact Planning fields that choice changes. Formal
Human Review rows/commands remain only for legacy recovery of records already held before this
change. Service/admin surfaces expose those compatibility continuations without making them a
current workflow route.

## Authority and data ownership

The authoritative workflow decision answers whether a consequential transition is legal for the current durable/live facts. Consumers may present, suppress, group, translate, or authorization-filter that result and may expose unrelated surface actions; they must not independently authorize a contradictory workflow transition.

Human approval is the authorization for the exact governed changes in an approved proposal. Applying that already-approved proposal is a later mutation step, not a second human-authorization decision for those same changes.

## Invariants

- Proposal approval and proposal application are separate durable actions.
- Human approval binds the exact proposal/governed-change bundle presented for review.
- Application of that exact approved bundle must not require a second human authorization for the same governed changes.
- The normal admin approval path durably records approval first, then Dish mechanically attempts application of that same immutable bundle; these remain two durable actions even when one operator command performs both in sequence.
- Low-level `apply-proposal` remains available for recovery/testing and for already-approved bundles whose normal mechanical application did not complete.
- Application may revalidate proposal identity, integrity, applicability, cycle/content binding, current authoritative facts, and concurrency before mutating.
- Application must not silently broaden, substitute, or alter the approved bundle.
- Candidate proposal content does not become canonical merely by being proposed or approved.
- Proposal validity/actionability is evaluated against governed semantic facts rather than irrelevant cosmetic/task metadata.
- Verification/review evidence stays bound to the exact subject it reviewed.
- Human Review and recovery continuations target durable recorded work rather than an ambiguous nearby operation.
- Current connected human input has two levels: ordinary clarification/preference and a deliberate
  choice recorded as an attributed `Human — Marco:` Decision. An appended Decision may attest only
  the exact Planning fields Marco just chose in that conversation; existing Decisions and provenance
  remain protected.
- Rewriting, removing, or reordering an existing attributed Decision remains governed and is not
  available through chat attestation.
- New connected-agent Verification never creates Evidence or Human Review holds. If a governed
  correction needs Marco's judgment, the agent asks in chat (or returns the question to its parent),
  appends the exact answer as a Decision, names the exact governed fields changed, and continues
  through the normal Large-correction/fresh-Verification path.
- Historical Human Review and Evidence rows remain recoverable through private admin compatibility
  commands. Their existence does not authorize creation of new held records.
- Semantic proposals already persisted before this change remain recoverable/applicable through the
  existing proposal/admin machinery; new connected Verification does not create them as a substitute
  for a chat Decision.
- A repeated-Verification cap is an operational loop detector, not a substantive Human Review decision. The third persisted rejected cycle moves the operation to the mechanical `verification_hold` phase. It reopens only through the admin `reopen` path with a canonical corrected candidate and an exact substantive reset (`evidence`, `premise`, `method`, or `scope`); an unchanged candidate cannot be reopened, and reopening neither approves nor authorizes the dish. The queue offers the same `reopen` for legacy loop holds in `held_human` / `pending-human-review`; the admin-only compatibility command `resolved` can still release such a legacy hold unchanged at Marco's own direction, which likewise neither approves nor authorizes the dish.
- Over-limit fat accepted by Marco is the one same-verifier exception: when the only changes are exactly one added `[nutrition-fat]` Exemptions line and exactly one appended `Human — Marco:` Decision, the same eligible verifier may sign. Any other change, including a construction edit, stays Large and needs fresh independent Verification.
- Small governed-text edits that may be incidental cleanup require explicit agent intent before they can enter the governed proposal path. This is an intent check, not a semantic classifier: the agent must restore incidental text exactly or explicitly identify an intended governed edit.
- "Allowed actions" are a derived view of authority, not a second state machine.
- Resting-task start authority is shared by discovery and admission: a task with no active operation
  exposes only the start kind that the same authority will accept. Post-signoff Change requires the
  exact current `ready` identity to resolve to durable approved signoff lineage; status text alone is
  never enough to authorize Change. A stranded imported `pending-verification` document does not
  imply Initial Research and exposes no ordinary start. Its only direct recovery is the private-admin,
  replay-bound `adopt-legacy-verification` command.
- Legacy Verification adoption is limited to one explicitly named, ordinary imported Dish whose exact
  current imported canonical content is valid, incomplete, unarchived, actively project-bound, and
  already in Verification Queue, with no conflicting live or unresolved authority. Adoption preserves
  content and placement, creates only a current Verification operation/cycle plus audit evidence, and
  does not claim that Research ran. Independent Verification then proceeds through the normal path.

## Process and transaction boundaries

Workflow policy may be evaluated in shared application/domain code while transports, renderers, persistence queries, and adapters perform their own legitimate responsibilities. The boundary is authority, not the presence or absence of conditionals.

## Normal flow

For semantic proposals, the durable lifecycle is:

1. create and validate the exact proposal against the governed semantic subject;
2. obtain human approval for that exact proposal;
3. persist that approval without yet changing canonical task content;
4. in the normal admin path, Dish mechanically claims/revalidates the exact approved proposal against current authoritative facts immediately after approval persistence, using an exact `(dish-mechanical, run_id)` principal and the canonical operation-execution mutation fence;
5. apply that proposal unchanged, or leave the durable approval intact and fail/reconcile if application is no longer safe or applicable. Low-level application can retry the same approved object without reapproval, but an explicitly revoked exact mechanical principal can never reclaim or apply it. A distinct successor run identity remains independently eligible.

For a current connected-agent Marco choice, the agent presents the real issue, concrete options and
the recommendation in ordinary conversation. Once Marco answers, the candidate appends that exact
choice as `Human — Marco:`; `governed_change_fields` names Decisions plus every Planning field
the answer intentionally changes. Verification Large corrections then remain pending Verification
for a fresh independent run. No held workflow or second approval ceremony is created.

Legacy Human Review/proposal records keep their historical recovery/application semantics only so
already-held production records remain recoverable.

More generally, workflow execution reads authoritative state, derives legal transitions, executes the selected transition through the owning application path, persists its outcome/evidence, then exposes resulting state/actions to callers.

## Failure, replay, recovery, and concurrency

Stale proposals, mismatched reviewed content, changed proposal identity/integrity, conflicting targets, or lost claims fail closed or move through explicit recovery. If the exact approved candidate is already canonical, recovery may reconcile that fact rather than demanding another human approval or blindly writing again.
Proposal-claim ownership is the exact `(owner_id, run_id)` principal. Matching a run UUID alone never grants claim visibility, application availability, lease admission, resume, or renewal authority.

Exact recovery mechanisms may evolve; durable identity, authorization, and no-silent-broadening guarantees are architectural, not today's function layout.

## Change routing

Change workflow legality at the authority that decides it. Change transport-specific guidance, presentation, argument collection, or recovery explanation in the relevant surface. Persistence helpers may legitimately query/shape required facts; they should not independently become workflow policy authority.

## Proving tests

Relevant evidence includes semantic proposal/Human Review workflow tests, workflow policy tests, connected-agent surface tests, and recovery/lease tests. Favor end-to-end behavioral agreement across surfaces over AST/source-text topology checks. Structural tests remain appropriate where a structural boundary itself protects authority or exposure.

## Current debt and temporary compatibility

Legacy Human Review/Evidence recovery and migration-era compatibility paths still coexist with the
current PostgreSQL workflow. They are recovery surfaces, not current connected-agent design.
Current recovery mechanics should remain documented until those historical records no longer need
support. Imported canonical records stranded at `pending-verification` are recovered through the
narrow adoption command above rather than by fabricating a Research pass.

## Related documents

- [Commands and surfaces](commands-and-surfaces.md)
- [Operations, leases, and fencing](operations-leases-and-fencing.md)
- [ADR-0003](decisions/0003-approval-and-application-are-separate.md)
