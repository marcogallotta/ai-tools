# Dish operator / orchestration control plane

This is the shared presentation and operator/orchestration mechanics contract referenced by the canonical Dish role index. It does not replace the mapped standing role, grant semantic or mutation authority, create a scheduler/queue/ownership service, or change Review/Integration/runtime authority.

All standing roles apply **Shared operator interaction**. Coordinator and Development Workflow additionally apply the task-specific control-plane sections below when performing those actions. Consequential writes still require the current explicit-intent policy and the mapped role's existing authority.

## Shared operator interaction

The generic work-chat behavior is sourced once from `dish/docs/chatgpt-projects/source.json` and generated into root `CLAUDE.md` plus every ChatGPT Project kernel. This document does not redefine that behavior; it adds only orchestration, authority, recovery, and readback mechanics.

Conversational execution never creates mutation authority. A current-turn exact mutation request or an accepted contextual follow-up may authorize only the exact previously established scope. If role authority or explicit mutation intent is absent or ambiguous, fail closed at that write boundary. Preserve the host's existing pre-tool/progress-update cadence; a progress message is not completion.

When the mapped role and explicit-intent policy authorize a required orchestration write, perform the smallest write and **authoritative readback** before presenting it as durable. Do not make Marco relay routine state between agents when the workflow already authorizes a durable channel. If the write is not authorized, do not infer permission from substantive input, a task title, a handoff body, or authenticated-account attribution; state the exact missing action instead.

Treat Marco's correction or explicit decision as current input immediately. Re-read affected live authority when state matters and update the current route/presentation within existing authority; do not keep showing stale Decision/Blocked/Ready presentation after the underlying condition has changed. Durable lifecycle classifications remain on their owning authority surface; the human message follows the generated Work chat contract.

### Bounded autonomous recovery

Within an already-authorized objective, a clearly recoverable routine failure is a continuation problem, not a new operator decision. Diagnose it, apply the smallest supported in-authority remediation, and retry the same failed operation without asking Marco to act as the retry button. This recovery rule creates no source authority, role composition, scheduler, database, queue, service, or control plane; the mapped standing role and current host authority remain controlling.

Automatic recovery is eligible only when **all** of the following are true:

1. the existing objective and exact governed candidate/target are unchanged;
2. the failure has a causal diagnosis as environmental, prerequisite, transient, or another non-semantic routine failure;
3. the remediation is an existing supported operation whose documented or otherwise established effect directly addresses that diagnosis;
4. the remediation is inside the active role and host authority;
5. the remediation is reversible or bounded and does not alter product, security, architecture, authority, or environment meaning;
6. no new credentials/login, destructive operation, production mutation, role composition, human approval, source-design decision, or material cost/risk decision is required;
7. the retry is the same failed operation against the same governed candidate/target; and
8. if the prior attempt could have changed state but its outcome is ambiguous, authoritative reconciliation first proves either that the intended effect already happened or that it did not happen and replay is safe/idempotent.

The recovery cycle is bounded and causal:

1. classify the failure before remedial action;
2. prove the eligibility conditions above;
3. reconcile any ambiguous prior state-changing outcome before replay;
4. capture or reuse the supported known-good pre-state/checkpoint when the remediation itself mutates local/runtime state;
5. perform the smallest supported causal remediation;
6. immediately rerun the same failed operation;
7. on PASS, continue the already-authorized objective without interrupting Marco;
8. on FAIL, reclassify the new evidence; a genuinely distinct newly exposed failure class may receive another cycle only if the prior cycle made demonstrable forward progress toward the same governing operation and the new class independently passes eligibility.

Recovery has two independent budgets. **Per causal failure class:** at most one diagnosed remediation plus one immediate retry; never repeat the same unresolved remediation loop. **Per governing failed operation/objective:** use any narrower existing operation-class budget defined by repository/runtime policy; otherwise allow at most **two distinct automatic recovery cycles**. Exhaustion stops deterministically. Do not evade either bound by relabeling an unresolved failure, splitting one cause into new names, or widening to adjacent defects.

A client-side timeout, disconnect, transport error, or missing acknowledgement never proves a state-changing operation failed. For Asana/GitHub writes or ref updates, child launches, runtime mutations, and any other operation that may be non-idempotent, perform authoritative readback/reconciliation before replay. If the intended effect is proven present, resume from observed state and **do not replay**. If absence is proven and replay is safe/idempotent, one retry may proceed within the remaining budget. If the outcome cannot be established or replay could duplicate/compound the mutation, fail closed and surface that unresolved boundary. Read-only/idempotent operations may use normal bounded transient retry/backoff within the same total recovery budget.

Stop autonomous recovery and surface the actual blocker when the same failure persists after its one remediation+retry, the governing-operation budget is exhausted, the alleged next class is not genuinely new or prior recovery made no forward progress, diagnosis admits materially different fixes, candidate/head/target moved, an ambiguous mutation cannot be reconciled, or the next action would cross semantic scope, security/product/architecture/authority, meaningful cost, credential/login, destructive/PROD, or consequential human-decision boundaries.

A source assertion/test failure is not automatically outside recovery, but this section never creates source mutation authority. An already-authorized Implementation role may diagnose/fix/retest an ordinary in-scope source bug under its standing authority for the same objective; a non-Implementation role may not use recovery as a route into source Implementation.

Persist only the attempt/failure information actually required to avoid duplicate recovery across replacement agents or destructive replay, using existing task/PR/controller/local durable state. Never add a retry database or alternate lifecycle authority merely to remember attempts.

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

## Authorized Implementation handoff state

When Coordinator or Development Workflow emits an **authorized** Implementation handoff, the handoff and owning Asana lifecycle write are one control-plane operation: move the task from `Ready` to `In Progress`, write one durable handoff record, and read both state and record back before claiming the handoff durable.

The durable record contains task GID, target role, handoff timestamp, handoff source, stable handoff identity, and branch/base plus PR/head when known. A branch still equal to base or no PR immediately after handoff is not evidence that the work was never handed off. Later GitHub branch/PR evidence is reconciled onto the same task/lineage.

At three hours after the durable handoff timestamp, if there is still no associated PR or other authoritative implementation evidence, surface exactly one `STALE HANDOFF — owner status unknown` observation for that handoff identity. The alert is observability only. It never authorizes duplicate dispatch, replacement, or a second writer. Before any ownership change, re-read live Asana + GitHub and reconcile the existing lineage.

`scripts/operator_handoff.py` encodes the write/readback and idempotent staleness predicates. It uses the existing Asana orchestration surface and GitHub lineage; it creates no queue, admission database, ownership service, or new control plane.

## Handoff executability preflight

Prepared handoff text is **DRAFT** until a terminal executability preflight succeeds. Before presenting any handoff as executable, copy-ready, or ready to send, resolve and read back every mandatory durable task/PR/branch/baseline identity, reject unresolved placeholder/template tokens, and verify the receiving standing role required by the role index. A sentence such as `ROLE: Audit` never changes a destination Project/session's standing authority.

If destination authority is unknown, the handoff is routing-required rather than executable. If the destination is known and incompatible, do not emit it as ready for that destination; return the single routing action, for example `send only to an Audit Project/session`. If a prerequisite requires a durable mutation not authorized by the standing explicit-intent policy, stop at `DRAFT / PREPARATION REQUIRED` and state that one missing action rather than creating it implicitly.

Where execution requires a fresh owning task, including an independent Audit round, verify that exact task exists, matches the required scope/baseline, and is distinct from any separate round before the handoff becomes executable. Two independent audit handoffs therefore require two distinct fresh task identities.

`scripts/handoff_preflight.py` is a transport-neutral fail-closed validator for these already-resolved inputs. It never creates a task, changes standing role authority, or performs a prerequisite write.

## Marco decisions versus external blockers

Use `MARCO DECISION — <priority> — <decision>` only when Marco's judgment or approval is the missing authority. Use `BLOCKED — <priority> — <dependency>` for task, PR, environment, external-system, or other mechanically owned dependencies. Deferred future work belongs in Backlog; an actionable investigation belongs in Ready/In Progress rather than being presented as a Marco decision.

Every Marco-decision task begins current notes with a compact Decision Packet bound to a stable decision identity/revision: **Decision needed**, **Recommended answer**, **Alternatives / material tradeoff**, **Consequence of no decision**, and **What happens immediately after approval**. A revised question is a new decision revision and cannot reuse old surfaced state.

Moving a current Marco decision into `Blocked / Decision`, or placing a Development Workflow V2 task whose next step is a genuine current human decision into `Needs Human Review`, creates an immediate surfacing obligation. Startup/status reconciliation also surfaces any current revision not yet durably surfaced. Persist the initial surfaced timestamp; if the same revision is still unresolved after 24 hours, surface it once more and no more. A changed decision question is a new revision. External blockers and prerequisite gates are never promoted into human decisions merely because Marco may eventually approve a later step.

When Marco explicitly answers the exact current decision revision, write that exact answer to the owning task, move it immediately to the correct next lifecycle section, and read both writes back in the same operation flow. A resolved decision must not remain presented in `Blocked / Decision`. Authenticated-account attribution alone never counts as Marco's decision.

For blocked items, keep the wake contract concrete: `BLOCKED ON`, `OWNER OF UNBLOCK`, `UNBLOCK WHEN`, and `THEN`. `scripts/operator_decision.py` encodes the one-shot surface, 24-hour reminder, exact-revision answer binding, and lifecycle readback without creating a human-approval service.

## Research-inclusive development triage

Pending-work triage always covers both implementation-ready and pre-implementation work. After live Asana/GitHub reconciliation, return exactly three operational buckets: **SEND NOW**, **NEEDS RESEARCH**, and **BLOCKED / WAITING**. Research/design/specification work remains distinct from Implementation; an under-specified task never enters SEND NOW merely because it is urgent.

For NEEDS RESEARCH, actively evaluate priority, dependencies, staleness, and whether the research itself is dispatchable now. Dispatchable research follows its normal role/owner without making Marco copy or route agent messages. Critical/high-consequence research is surfaced proactively when it changes Marco's action or decision; lower-priority research remains queued without repeated interruption. Once research resolves the missing design/specification, require the normal durable readiness/review gate before Implementation.

Every triage pass reconciles obvious stale labels before suggesting work: a Ready item already resolved/implemented, a research item whose prerequisite cleared, or a blocked item whose wake predicate is satisfied is corrected to current lifecycle truth.

`scripts/operator_triage.py` supplies the deterministic fail-closed three-bucket projection after those live authority reads. It does not create a scheduler, queue, ownership service, or semantic dispatch authority.
