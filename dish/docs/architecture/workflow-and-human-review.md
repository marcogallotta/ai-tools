# Workflow and human review

## Read this when

Read this for Planning, Research, Verification, approval, rejection, holds, Human Review, semantic proposals, submission, abandonment, succession, or current-action changes.

## Scope

This document owns the current action-authority model and workflow/human-review boundaries. It does not own transport parsing, exact Asana I/O, or request replay mechanics.

## Authoritative implementation

- Snapshot and mutation authority: `dish_tool/application_service.py`.
- Pure legal-action policy: `dish_tool/workflow_policy.py`.
- Agent command application: `dish_tool/commands.py`.
- Admin application: `dish_tool/admin.py`.
- Stage use cases: `dish_tool/step5.py`, `dish_tool/step6.py`, `dish_tool/step7.py`, `dish_tool/step8.py`, `dish_tool/step9.py`.
- Holds and human actions: `dish_tool/hold_resolution.py`, `dish_tool/human_actions.py`, `dish_tool/review_queue.py`.
- Semantic proposal lifecycle: `dish_tool/semantic_proposals.py`.
- Abandonment and succession: `dish_tool/abandonment.py`, `dish_tool/abandonment_succession.py`, `dish_tool/operation_execution.py`.
- Durable workflow tables and invariants: `dish_tool/database_schema.py`, `dish_tool/database.py`.

## Actors, processes, and stores

Actors are planner/researcher, verifier, material editor, Marco/admin, and service/recovery. Their identities and run lineage are durable facts. The live task is the document under review; SQLite owns operations, actor facts, cycles, holds, signoff, proposals, and recovery state.

## Authority and data ownership

`CurrentWorkflowService._snapshot` reads the operation, exact live task, current section registry, cycle, actor facts, pending steps, unresolved attempts, and signoff/hold evidence. `workflow_policy.legal_actions` is the only owner of the current legal action list. Stage modules own the transition side effects. Admin continuations target exact recorded operations, cycles, holds, proposals, or abandonment attempts.

## Invariants

- There is one open/active operation authority per task under the current schema constraints.
- Legal actions are derived from one exact snapshot; no caller maintains a parallel state-to-action matrix.
- Verification binds an independent verifier agent/run, exact reviewed identity, current cycle, inspection fact, and signoff lineage.
- Approval is not submission. Submission requires the signed Ready identity and handles destination movement separately.
- Evidence and Human Review holds preserve exact baselines and expose only the recorded continuation.
- Proposal approval is separate from proposal application; application consumes the exact approved bundle and remains auditable.
- Abandonment is not generic lease expiry. It is a route-preserving recovery workflow with exact successor/target evidence.
- A prepared successor claim cannot be replaced with a nearby open operation or later cycle.

## Process and transaction boundaries

The action snapshot is built before mutation and revalidated inside command execution. Stage operations use SQLite savepoints/writer transactions and durable operation steps. External effects are separately journaled. Abandonment succession is one immutable spec applied in one local writer transaction. PostgreSQL preparation/discard uses the workflow-operation row lock as the serialization boundary.

## Normal flow

1. Start records an operation and actor/run lineage; Planning uses a durable two-request intent challenge.
2. Prepare validates a complete candidate and records content/placement intent and the Verification handoff.
3. Verification start binds the exact verifier and reviewed content occurrence.
4. Inspect records the current review occurrence.
5. Approve records signoff (and an exact small correction when applicable); reject opens a Large route or a named hold.
6. Human Review/admin commands resolve only the exact durable hold or proposal.
7. Submit validates the signed Ready task and completes or recovers destination handling.
8. Abandon/reconcile is used only for dead-run recovery frontiers that cannot be handled as ordinary lease reclaim.

## Failure, replay, recovery, and concurrency

Content or placement drift removes legal actions. Pending steps and unresolved attempts also fail closed. A crash after a workflow commit but before service-request completion is recovered from the operation execution and stored workflow result, not by repeating the transition. Holds and abandonment attempts are task-level mutation fences. Concurrent same-task starts, preparations, claims, or discards are serialized by database constraints/locks; independent tasks remain independent.

## Change routing

- Change state/action predicates in `dish_tool/workflow_policy.py` and snapshot inputs in `dish_tool/application_service.py`.
- Change one stage transition in its owning `stepN.py` module and shared domain helper.
- Add a human continuation only with an exact persisted target and a typed admin command specification.
- Add proposal behavior in `semantic_proposals.py` and review/application surfaces; do not turn approval into immediate hidden application.
- Do not implement workflow rules in HTTP, CLI rendering, Asana adapters, or persistence query helpers.

## Proving tests

- `tests/test_workflow_policy_fail_closed.py` proves legal-action fail-closed predicates.
- `tests/test_action_full_lifecycle.py` proves the end-to-end current workflow.
- `tests/test_verification_atomicity_and_route_recovery.py` proves review/signoff routing and recovery.
- `tests/test_human_review_queue_workflow.py` and `tests/test_semantic_proposal_bundle_workflow.py` prove asynchronous Human Review and proposal application.
- `tests/test_abandonment_admin_workflow.py`, `tests/test_abandonment_fencing_and_reconciliation.py`, and `tests/test_abandonment_stage_successors.py` prove route-preserving recovery.
- `tests/test_change_start_intent.py` and `tests/test_planning_intent_concurrency_and_surfaces.py` prove durable intent gates.

## Current debt and temporary compatibility

The current SQLite workflow remains document-and-Asana-coupled. PostgreSQL target workflow is implemented separately and must remain semantically equivalent until cutover. Some future product decisions in `docs/workflow.md` and PostgreSQL planning documents are not current behavior. Legacy `submissions` data remains inspectable/migratable but is not an executable workflow engine.

## Related documents

- [Authority and data ownership](authority-and-data-ownership.md)
- [Commands and surfaces](commands-and-surfaces.md)
- [External effects and Asana](external-effects-and-asana.md)
- [ADR-0003](decisions/0003-approval-and-application-are-separate.md)
