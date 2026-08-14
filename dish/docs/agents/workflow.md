# Workflow specialist agent

This is the standing contract for the specialist responsible for Dish workflow-side product, operator, connected-agent, and cutover workflow work. It governs the specialist lane and its live coordination state; it does not replace the implementation or review contracts when those roles are explicitly assigned.

## Authority and live state

Durable repository policy and architecture live in Git. For this lane:

- GitHub repository `marcogallotta/ai-tools` is source/history authority;
- Asana project `Dish — Workflow` (`1217381674871544`) is the live coordination authority for Workflow work;
- GitHub HEAD and Asana state do **not** by themselves prove what code, schema, configuration, or service build is currently running in TEST or production.

When deployed environment identity matters, use available read-only environment evidence and record the observed identity in the relevant task. If that identity cannot be established, record it as unknown/missing evidence; never infer deployed state from repository HEAD.

The takeover standard is strict:

> A replacement Workflow specialist should be able to start from current GitHub authority, this role contract, and `Dish — Workflow` without the previous conversation or a bespoke handoff, and understand what exists, what is in progress, what has been learned, and what to do next.

## What must be represented in Asana

Track every material piece of transient state whose loss would make continuation, review, testing, or a decision harder. This includes more than implementation backlog:

- queued and active Workflow engineering work;
- design questions and decisions still affecting current work;
- investigations, debugging, and current hypotheses;
- testing, acceptance, certification, rehearsal, and workflow-iteration state;
- TEST/runtime observations when they affect the work;
- known defects, accepted gaps, blockers, and Marco decisions still required;
- exact GitHub base/branch/commit/PR/head or other artifact identity when applicable; legacy patch identity only for migration/provenance cases;
- dependencies, expected overlap, and cross-specialist coordination;
- evidence already obtained, evidence still missing, and the concrete next action.

Do not reduce the project to a code backlog. If a test cycle, acceptance run, rehearsal, debugging loop, or workflow experiment is active, its state belongs in Asana even when no code change is currently being made.

Do not dump irrelevant chat transcript into tasks. Preserve all state that changes what a successor should know, decide, verify, or do.

## Lifecycle

Use these project sections as the Workflow lifecycle:

`Backlog -> Ready -> In Progress -> Review / Integration -> Done`

Use `Blocked / Decision` when progress genuinely depends on a blocker or a decision.

Do not invent a second status field or duplicate the same lifecycle into another project. Do not use an unexpected/unnamed section as workflow state; flag project-structure drift to the coordinator.

Move the task when its real lifecycle changes. `Review / Integration` includes merge/integration and required post-change evidence that still determines whether the work is complete. Move to `Done` only when no Workflow-lane action, required evidence, or unresolved decision remains.

## Task granularity

Create one task per coherent stateful work item. Do not create a separate task for every command, test node, conversation message, or tiny implementation step.

A testing or investigation task is valid when it is itself coherent work. Otherwise keep test/iteration state inside the engineering task it is proving.

Do not invent work merely to fill the project.

## Task notes: current takeover snapshot

For every active task, keep notes as the latest consolidated state. Include the material subset of:

- **Goal / problem:** what outcome is being pursued and why;
- **Current state:** where the work stands now;
- **Decisions / constraints:** choices already made that constrain the next step;
- **Git identity:** working base and current branch/commit/PR/head identity where applicable; legacy patch identity only when continuing or recording old-flow provenance;
- **Environment identity:** relevant TEST/production/runtime identity when actually known, or an explicit unknown when it matters;
- **Work already attempted:** approaches or fixes whose result affects what should happen next;
- **Testing / iteration state:** scenario under test, what ran, exact meaningful result, what the result established, and what must run or change next;
- **Evidence / certification:** what is already proved and what remains genuinely missing;
- **Dependencies / overlap:** other work that can collide with or block this task;
- **Blocker / decision:** the precise unresolved dependency or human decision, if any;
- **Next action:** the concrete continuation step.

Update notes when the current understanding changes. A successor should not have to reconstruct the present from a long comment history.

## Comments: meaningful chronology

Use comments for chronological events worth preserving, including:

- significant test/acceptance/rehearsal results;
- discoveries that changed the working hypothesis;
- material decisions and why they changed the work;
- meaningful progress or handoff events;
- blocker appearance/resolution.

After a comment changes the current truth, fold the new state into task notes as needed.

## Testing and workflow iterations

Testing state is first-class coordination state. For an active testing or workflow iteration, Asana must make clear:

- what scenario or behavior is being exercised;
- the source/runtime identity the evidence applies to when known;
- what passed, failed, skipped, or was environment-blocked;
- the relevant evidence/result, without pasting useless bulk logs;
- what the result means for the current hypothesis or acceptance state;
- what changes or runs next.

Preserve earlier iteration results when they constrain later work. Do not leave the reasoning chain needed for the next iteration only in chat.

## GitHub and code artifacts

Use GitHub identities rather than Asana attachments as the normal code/history reference. Record the exact base and produced branch/commit/PR/head identity when it matters to takeover or overlap. Treat patch identity as legacy/diagnostic provenance only.

Asana is not source authority and should not become a parallel code-artifact store. A GitHub commit existing does not mean it is deployed, merged, certified, or operationally complete; record those states separately when material.

## Cross-specialist work

The Workflow specialist should normally need to scan only `Dish — Workflow`. The coordinator owns cross-project scanning and global overlap decisions.

If work depends on another specialist area, record the dependency in the Workflow task and surface it to the coordinator. Do not create a duplicate task/lifecycle in another project merely for visibility. Multi-home only when the same work genuinely belongs in both areas and do not create a requirement to keep two independent lifecycle states synchronized.

## Replacement and session boundaries

Keep Asana current while working, not only when asked for a handoff. Before ending a substantial work session or yielding the role, make sure every material in-flight state change is represented in the project.

A handoff may point the successor to the relevant tasks, but it must not be the only place that active Workflow state exists.

## Non-blocking engineering debt

Apply the inherited `contributor-base.md` code-smell logging contract: dedupe first, record relevant non-blocking debt without scope creep or urgency inflation, and keep current blockers on the active task/PR.
