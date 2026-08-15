# Dish operator / orchestration control plane

This is the shared presentation and operator/orchestration mechanics contract referenced by the canonical Dish role index. It does not replace the mapped standing role, grant semantic or mutation authority, create a scheduler/queue/ownership service, or change Review/Integration/runtime authority.

All standing roles apply **Shared operator interaction**. Coordinator and Development Workflow additionally apply the task-specific control-plane sections below when performing those actions. Consequential writes still require the current explicit-intent policy and the mapped role's existing authority.

## Shared operator interaction

Classify the current operator turn before choosing presentation: **EXECUTE**, **ANSWER**, **STATUS**, **DISCUSS / DECIDE**, **RESEARCH**, or **CORRECTION**. This is behavioral classification only: **EXECUTE is not mutation authority**. A current-turn exact mutation request or an accepted contextual follow-up may authorize only the exact previously established scope. If role authority or explicit mutation intent is absent or ambiguous, fail closed at that write boundary.

Apply these invariants:

- **say-do** — perform an authorized requested action before describing it; progress narration never substitutes for the action;
- **answer-first** — put the direct answer first instead of leading with recap, generic acknowledgement, or internal process narration;
- preserve the host's existing pre-tool/progress-update cadence; this rule does not remove, lengthen, or weaken it;
- suppress standalone acknowledgements/meta commentary that do not change Marco's action or understanding;
- design/research chat carries the proposal, recommendation, decision, blocker, or next research action; durable detail belongs in the owning authority surface when a write is authorized;
- use a **substantive operator-attention gate**: interrupt Marco only for information that changes his next action, decision, obligation, or understanding of a material result.

When a lifecycle result needs a human-facing label, use the smallest accurate presentation class: **COMPLETE / LANDED**, **CONTINUE AUTOMATICALLY**, **FIX REQUIRED**, **WAITING**, **MANUAL ACTION**, **HUMAN DECISION**, or **TRUE BLOCKER**. These labels are **presentation only** and never create a second lifecycle. Preserve the exact next owner/action and the gate or wake predicate that makes the label true.

When the mapped role and explicit-intent policy authorize a required orchestration write, perform the smallest write and **authoritative readback** before presenting it as durable. Do not make Marco relay routine state between agents when the workflow already authorizes a durable channel. If the write is not authorized, do not infer permission from substantive input, a task title, a handoff body, or authenticated-account attribution; state the exact missing action instead.

Treat Marco's correction or explicit decision as current input immediately. Re-read affected live authority when state matters and update the current route/presentation within existing authority; do not keep showing stale Decision/Blocked/Ready presentation after the underlying condition has changed. Use normal engineering language and keep internal jargon only where it identifies real durable state, owner, gate, exact PR/head, or required action.

## TRUE READY dispatch queue

`Ready` is a dispatchable queue, not a holding area. A task belongs there only when it is dispatchable now: **no unresolved Asana dependency**, **no pending Marco-only decision**, **no required prior design/readiness review**, and **no known active competing implementation lineage**. A stale Ready placement never authorizes dispatch.

Blocked work stays out of Ready. Preserve the real Asana dependency where available and add the smallest stable task-name marker such as `[blocked on <gid>]`. Implementation-ready tasks carry one or a few stable coarse areas using the smallest existing Asana representation, for example `CODE AREA: lifecycle/CI, agent tooling`. Code area is a **first-pass overlap hint only** and **cannot authorize work by itself**; missing/stale metadata never authorizes dispatch.

Coordinator's ordinary next-work path is deliberately narrow:

1. read maintained Ready;
2. compare priority/urgency;
3. compare coarse code areas with current In Progress implementation work;
4. perform **one live sanity check** on the selected candidate's dependencies, human-decision/review gates, and competing GitHub lineage;
5. dispatch only if that live check agrees.

A contradiction discovered during the sanity check is reconciled out of Ready rather than ignored. Do not rebuild merged history or long-note provenance unless maintained state is inconsistent. Ready -> In Progress on actual handoff and post-merge reconciliation remain owned by their existing lifecycle mechanics. This policy creates **no scheduler, second queue, or ownership service**. Do not create a specialist scheduler.
