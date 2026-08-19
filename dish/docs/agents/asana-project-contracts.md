# Asana project operating-contract routing

This is the shared routing contract for every direct or tool-mediated Asana mutation performed by
a Dish standing role or Worker mode. It identifies the target project and routes the write to the
repository-owned contract for that project. It does not grant mutation authority, combine roles,
or replace the target project's own lifecycle and field semantics.

## Universal write procedure

Immediately before an otherwise-authorized Asana write:

1. Resolve the exact target project GID. For an existing task, read complete project memberships
   and select the intended project by exact GID, never by first membership or fuzzy name. For task
   creation, require the explicit destination project GID.
2. Freshly read this router from current Git authority and select exactly one registered contract
   below. Then read the target contract's relevant lifecycle, field, note, comment, and completion
   rules before writing. Previously loaded Project/session wording is not current write authority.
3. Preserve the selected project's semantics. Never project Development Workflow V2 sections,
   fields, or readiness meanings into another project.
4. Perform only the write already authorized by the active role/task. This router never supplies
   missing role, task, production, destructive-operation, or human-decision authority.
5. Authoritatively read back every state-changing write and verify the intended project, section,
   fields, content, and completion state that the operation changed before reporting success.

A bounded multi-operation write may reuse one fresh routing read only while its exact target
project, current contract revision, authorization, and intended semantics remain unchanged. A
different target project or a re-ground/session discontinuity requires routing again.

If the target project is absent from the registry, the project identity is ambiguous, its mapped
contract cannot be freshly read, or live structure contradicts that contract, perform zero
mutation. Record or route the missing/contradictory contract through the existing Development
Workflow process; do not guess a lifecycle or use the nearest project's rules.

## Registered agent coordination projects

| Exact project | Repository-owned operating contract |
|---|---|
| `Dish — Coordinator` (`1217382473444945`) | [`coordinator.md#Asana live coordination`](coordinator.md#asana-live-coordination) |
| `Dish — Workflow` (`1217381674871544`) | [`workflow.md#Lifecycle`](workflow.md#lifecycle), [`workflow.md#Task notes: current takeover snapshot`](workflow.md#task-notes-current-takeover-snapshot), and [`workflow.md#Comments: meaningful chronology`](workflow.md#comments-meaningful-chronology) |
| `Dish — PostgreSQL / Dark Launch` (`1217404747383060`) | [`postgresql-dark-launch.md#Lifecycle`](postgresql-dark-launch.md#lifecycle), [`postgresql-dark-launch.md#Task notes: current takeover snapshot`](postgresql-dark-launch.md#task-notes-current-takeover-snapshot), and [`postgresql-dark-launch.md#Comments: meaningful chronology`](postgresql-dark-launch.md#comments-meaningful-chronology) |
| `Dish — Development Workflow` / `Dish — Development Workflow v2` (`1217419962189616`) | [`development-workflow-asana-mode.md`](development-workflow-asana-mode.md), including its exact generation classification and V2 lifecycle/field rules |
| `Dish — Development Workflow Friction` (`1217443500915644`) | [`contributor-base.md#Development Workflow Friction capture`](contributor-base.md#development-workflow-friction-capture) |
| `Dish — Code Smells / Engineering Debt` (`1217443501022227`) | [`contributor-base.md#Code-smell / engineering-debt logging`](contributor-base.md#code-smell--engineering-debt-logging) |

The Coordinator may observe multiple registered projects, but cross-project visibility does not
make one project's section placement or fields authoritative for another. Multi-homed tasks are
interpreted separately for each exact registered membership.

## Cooking project mutations

The configured Cooking projects are also registered targets:

- TEST: `1216693403164366`;
- production: `1217084805070730`.

Their operating contract is the Dish command/workflow authority in [`../../README.md#Service-host
configuration`](../../README.md#service-host-configuration), [`../workflow.md`](../workflow.md),
and [`../runtime-contract.md`](../runtime-contract.md), together with the current Honest
task-document assets resolved by the service. Agents mutate Cooking tasks through the governed Dish
CLI/Action/admin surface appropriate to their authority, not through generic direct Asana writes.
The selected profile must return the expected exact Cooking project GID. TEST and production
identity and mutation authority remain separate; this router grants neither production nor admin
authority.

If a future deployed Cooking profile or another agent-mutated Asana project is introduced, add its
exact identity and canonical contract here before agents write to it. Test fixtures and disposable
rehearsal projects do not become production mutation targets merely because their GIDs appear in a
runbook.
