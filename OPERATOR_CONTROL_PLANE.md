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
